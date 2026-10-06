"""Clone workspaces, run Codex in task images, and collect side results."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path

import relay
from paths import CODEX_HOME, CODEX_LINUX, EXPECTED_WINDOW, MODEL, MODEL_PROVIDER, ROOT, RUNS, WORK
from records import load_task, task_record_path, task_type_from_categories, write_json

_pull_lock = threading.Lock()
_pulled: set[str] = set()
_image_users: dict[str, int] = {}
_HOOKS = ROOT / "harness" / "empty-hooks"
_ILLEGAL_CHARS = set('<>:"|?*')
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"}
CODEX_TIMEOUT_SECONDS = 2 * 60 * 60


def run_cmd(
    args: list[str],
    cwd: Path | None = None,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    merged = None
    if env:
        merged = os.environ.copy()
        merged.update(env)
    return subprocess.run(
        args,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
        env=merged,
        input=input,
    )


def run_visible(args: list[str]) -> None:
    print(" ".join(args), flush=True)
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    tail: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        text = line.strip()
        if text:
            tail.append(text)
            if len(tail) > 20:
                del tail[:-20]
    code = proc.wait()
    if code != 0:
        detail = " | ".join(tail[-6:])
        message = f"command failed ({code}): {' '.join(args)}"
        if detail:
            message = f"{message}: {detail}"
        raise RuntimeError(message)


_NETWORK_MARKERS = (
    "recv failure",
    "connection was aborted",
    "connection reset",
    "failed to connect",
    "could not resolve host",
    "couldn't resolve host",
    "the remote end hung up",
    "rpc failed",
    "early eof",
    "timed out",
    "timeout",
    "tls",
    "ssl",
    "curl 56",
    "curl 18",
    "curl 28",
    "gnutls",
    "empty reply from server",
    "connection refused",
    "network is unreachable",
    "transfer closed",
    "dial tcp",
    "i/o timeout",
    "request canceled",
    "unexpected eof",
    "cannot lock ref",
)


def _network_error(message: str) -> bool:
    text = message.lower()
    return not text or any(marker in text for marker in _NETWORK_MARKERS)


def call_with_retry(action, label: str, attempts: int = 5, network_only: bool = False):
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except RuntimeError as exc:
            if attempt == attempts or (network_only and not _network_error(str(exc))):
                raise
            wait = min(60, 10 * 2 ** (attempt - 1))
            print(f"{label} retry in {wait}s attempt {attempt + 1}/{attempts}", flush=True)
            time.sleep(wait)


def git(
    repo: Path,
    *args: str,
    timeout: int | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run git with repo hooks disabled. Checkout must not fail on husky.

    autocrlf and symlinks are forced off so a Windows host does not dirty the
    tree or abort on a symlink the process is not allowed to create.
    """
    _HOOKS.mkdir(parents=True, exist_ok=True)
    return run_cmd(
        [
            "git",
            "-c",
            f"core.hooksPath={_HOOKS}",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.symlinks=false",
            "-c",
            "core.longpaths=true",
            "-C",
            str(repo),
            *args,
        ],
        timeout=timeout,
        input=input,
        env={"HUSKY": "0", "HUSKY_SKIP_HOOKS": "1", "FILTER_BRANCH_SQUELCH_WARNING": "1"},
    )


def windows_illegal(path: str) -> bool:
    for part in path.replace("\\", "/").split("/"):
        if part in ("", ".", ".."):
            continue
        if part.endswith(".") or part.endswith(" "):
            return True
        if any(char in part for char in _ILLEGAL_CHARS):
            return True
        stem = part.split(".", 1)[0].upper()
        if stem in _RESERVED_NAMES:
            return True
        if len(stem) == 4 and stem[:3] in {"COM", "LPT"} and stem[3].isdigit():
            return True
    return False


def skip_illegal_windows_paths(repo: Path, sha: str) -> None:
    """Leave Windows-illegal paths out of the worktree via a cone sparse-checkout."""
    listed = git(repo, "ls-tree", "-r", "--name-only", sha)
    if listed.returncode != 0:
        raise RuntimeError(listed.stderr.strip() or "git ls-tree failed")
    names = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    illegal = [name for name in names if windows_illegal(name)]
    if not illegal:
        return
    root_illegal = [name for name in illegal if "/" not in name]
    if root_illegal:
        raise RuntimeError("windows cannot checkout " + ", ".join(root_illegal))
    blocked: set[str] = set()
    all_dirs: set[str] = set()
    for path in names:
        acc = ""
        for part in path.split("/")[:-1]:
            acc = f"{acc}/{part}" if acc else part
            all_dirs.add(acc)
    for path in illegal:
        acc = ""
        for part in path.split("/")[:-1]:
            acc = f"{acc}/{part}" if acc else part
            blocked.add(acc)
    include: list[str] = []
    for directory in sorted(all_dirs):
        if directory in blocked:
            continue
        parent = directory.rsplit("/", 1)[0] if "/" in directory else ""
        if parent and parent not in blocked:
            continue
        include.append(directory)
    print(f"skip {len(illegal)} windows-illegal paths under {repo}", flush=True)
    init = git(repo, "sparse-checkout", "init", "--cone", "--sparse-index")
    if init.returncode != 0:
        raise RuntimeError(init.stderr.strip() or "sparse-checkout init failed")
    selected = git(repo, "sparse-checkout", "set", "--stdin", "--cone", input="\n".join(include) + "\n")
    if selected.returncode != 0:
        raise RuntimeError(selected.stderr.strip() or "sparse-checkout set failed")


def force_checkout(repo: Path, sha: str, branch: str | None) -> None:
    clear_git_locks(repo)
    skip_illegal_windows_paths(repo, sha)
    # -ff removes nested repositories. -fdx leaves them, and status then
    # reports the tree dirty at base_commit.
    cleaned = git(repo, "clean", "-ffdx")
    if cleaned.returncode != 0:
        raise RuntimeError(cleaned.stderr.strip() or "git clean failed")
    if branch:
        checked = git(repo, "checkout", "-f", "-B", branch, sha)
    else:
        checked = git(repo, "checkout", "-f", "--detach", sha)
    if checked.returncode != 0:
        detail = (checked.stderr or checked.stdout).strip()
        raise RuntimeError(detail or "git checkout failed")
    _materialize_symlinks(repo, sha)


def _materialize_symlinks(repo: Path, sha: str) -> None:
    """Replace symlink placeholders with the target file.

    Windows refuses to create symlinks without extra privilege, so checkout
    writes the link text. Copy the target and tell git to keep the index entry
    as a symlink, otherwise status reports the tree dirty.
    """
    listed = git(repo, "ls-tree", "-r", "-z", sha)
    if listed.returncode != 0:
        raise RuntimeError(listed.stderr.strip() or "git ls-tree failed")
    for record in listed.stdout.split("\0"):
        if "\t" not in record:
            continue
        meta, path = record.split("\t", 1)
        if not meta.startswith("120000"):
            continue
        link = repo / path
        if not link.is_file():
            continue
        raw = link.read_bytes().splitlines()
        if not raw:
            continue
        target = raw[0].decode("utf-8", errors="replace").strip()
        if not target:
            continue
        dest = Path(target)
        if not dest.is_absolute():
            dest = link.parent / dest
        try:
            dest = dest.resolve()
        except OSError:
            continue
        if dest == link.resolve():
            continue
        if dest.is_file():
            shutil.copyfile(dest, link)
        elif dest.is_dir():
            link.unlink()
            shutil.copytree(dest, link)
        else:
            continue
        marked = git(repo, "update-index", "--assume-unchanged", "--", path)
        if marked.returncode != 0:
            detail = (marked.stderr or marked.stdout).strip()
            raise RuntimeError(detail or f"could not keep symlink {path}")


def remove_tree(path: Path) -> None:
    def onerror(func, target, exc_info):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onerror=onerror)


def _git_dir(repo: Path) -> Path:
    git_path = repo / ".git"
    if git_path.is_file():
        line = git_path.read_text(encoding="utf-8", errors="replace").strip()
        if line.lower().startswith("gitdir:"):
            raw = Path(line.split(":", 1)[1].strip())
            if not raw.is_absolute():
                raw = repo / raw
            return raw
    return git_path


def clear_git_locks(repo: Path) -> None:
    """Drop leftover lock files so a crashed git does not block the next checkout."""
    git_dir = _git_dir(repo)
    if not git_dir.is_dir():
        return
    for name in ("index.lock", "HEAD.lock", "shallow.lock", "config.lock", "packed-refs.lock"):
        lock = git_dir / name
        if lock.is_file():
            lock.unlink()
    refs = git_dir / "refs"
    if refs.is_dir():
        for lock in refs.rglob("*.lock"):
            if lock.is_file():
                lock.unlink()


def _ignore_git_locks(directory: str, names: list[str]) -> set[str]:
    if ".git" in Path(directory).parts:
        return {name for name in names if name.endswith(".lock")}
    return set()


def prepare(row: dict, meta_extra: dict, force: bool, retry: bool, keep: set[str] | None = None) -> str:
    instance_id = row["instance_id"]
    sha = row["base_commit"]
    prompt = row["problem_statement"]
    keep = keep or set()
    existing_sides = (load_task(instance_id).get("sides") or {})
    write_json(
        task_record_path(instance_id),
        {
            "instance_id": instance_id,
            "repo": row["repo"],
            "base_commit": sha,
            "language": row.get("language") or meta_extra.get("language") or "",
            "image_name": meta_extra["image_name"],
            "test_cmd": meta_extra.get("test_cmd") or "",
            "problem_statement": prompt,
            "task_type": task_type_from_categories(row.get("pr_categories")),
            "difficulty": "困难",
            "os": "Windows",
            "repro_level": "已容器化，可一键起环境",
            "initial_permalink": None,
            "delivery_a": None,
            "delivery_b": None,
            "gsb": None,
            "gsb_reason": None,
            "sides": {side: existing_sides[side] for side in keep if side in existing_sides},
        },
    )
    src = WORK / instance_id / "src"
    if force and src.exists():
        remove_tree(src)
    if not src.exists():
        def clone_once() -> None:
            if src.exists():
                remove_tree(src)
            src.parent.mkdir(parents=True, exist_ok=True)
            _HOOKS.mkdir(parents=True, exist_ok=True)
            run_visible(
                [
                    "git",
                    "-c",
                    f"core.hooksPath={_HOOKS}",
                    "-c",
                    "http.lowSpeedLimit=1000",
                    "-c",
                    "http.lowSpeedTime=60",
                    "clone",
                    "--no-checkout",
                    "--progress",
                    f"https://github.com/{row['repo']}.git",
                    str(src),
                ]
            )

        call_with_retry(clone_once, "git clone")
    clear_git_locks(src)
    force_checkout(src, sha, None)
    for side in ("a", "b"):
        if side in keep:
            continue
        side_dir = WORK / instance_id / side
        branch = side_branch(instance_id, side)
        if force and side_dir.exists():
            remove_tree(side_dir)
        if not side_dir.exists():
            shutil.copytree(src, side_dir, ignore=_ignore_git_locks)
        clear_git_locks(side_dir)
        force_checkout(side_dir, sha, branch)
        head = git(side_dir, "rev-parse", "HEAD")
        dirty = git(side_dir, "status", "--porcelain")
        if head.stdout.strip() != sha or dirty.stdout.strip():
            raise RuntimeError(f"{side} is not a clean tree at base_commit")
    return prompt


def docker_pull(image: str) -> None:
    with _pull_lock:
        if image in _pulled:
            return
        call_with_retry(lambda: run_visible(["docker", "pull", image]), "docker pull")
        _pulled.add(image)


def image_acquire(image: str) -> None:
    with _pull_lock:
        _image_users[image] = _image_users.get(image, 0) + 1


def image_release(image: str, remove: bool) -> None:
    with _pull_lock:
        left = _image_users.get(image, 1) - 1
        if left > 0:
            _image_users[image] = left
            return
        _image_users.pop(image, None)
        if not remove:
            return
        _pulled.discard(image)
    try:
        removed = run_cmd(["docker", "rmi", image])
    except Exception as exc:  # noqa: BLE001 - cleanup must not change the task result
        print(f"image remove failed {image}: {exc}", flush=True)
        return
    if removed.returncode != 0:
        detail = (removed.stderr or removed.stdout).strip()
        print(f"image remove failed {image}: {detail}", flush=True)


def side_codex_home(instance_id: str, side: str) -> Path:
    return WORK / instance_id / f".codex-{side}"


def prepare_codex_home(instance_id: str, side: str) -> Path:
    """Private Codex home so parallel containers never share config.toml."""
    home = side_codex_home(instance_id, side)
    home.mkdir(parents=True, exist_ok=True)
    base_url = relay.proxy_base(instance_id, side)
    (home / "config.toml").write_text(
        "\n".join(
            [
                f'model_provider = "{MODEL_PROVIDER}"',
                f'model = "{MODEL}"',
                f"model_context_window = {EXPECTED_WINDOW}",
                "model_auto_compact_token_limit = 950000",
                'model_instructions_file = "/root/.codex/relay_base_instructions.txt"',
                'model_catalog_json = "/root/.codex/relay_model_catalog.json"',
                "",
                f"[model_providers.{MODEL_PROVIDER}]",
                'name = "OpenAI"',
                f'base_url = "{base_url}"',
                'wire_api = "responses"',
                'env_key = "SUPER_RELAY_KEY"',
                "request_max_retries = 0",
                "",
            ]
        ),
        encoding="utf-8",
    )
    for filename in ("relay_base_instructions.txt", "relay_model_catalog.json", "auth.json"):
        source = CODEX_HOME / filename
        if source.is_file():
            shutil.copy2(source, home / filename)
    auth = home / "auth.json"
    if not auth.is_file():
        auth.write_text('{"OPENAI_API_KEY":"sk-placeholder"}\n', encoding="utf-8")
    return home


def codex_container_name(instance_id: str, side: str) -> str:
    return f"gsb-codex-{instance_id}-{side}"


def remove_codex_container(instance_id: str, side: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", codex_container_name(instance_id, side)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def run_codex(instance_id: str, side: str, image: str, prompt: str, log_path: Path) -> int:
    work_dir = WORK / instance_id / side
    container_dir = f"/work/{instance_id}/{side}"
    codex_home = prepare_codex_home(instance_id, side)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "export PATH=/opt/gsb-codex/bin:$PATH; "
        "exec codex exec --dangerously-bypass-approvals-and-sandbox "
        f"--config model_provider={MODEL_PROVIDER} --model {MODEL} "
        f"--config model_providers.{MODEL_PROVIDER}.name=OpenAI "
        f"--config model_providers.{MODEL_PROVIDER}.base_url={relay.proxy_base(instance_id, side)} "
        f"--config model_providers.{MODEL_PROVIDER}.wire_api=responses "
        f"--config model_providers.{MODEL_PROVIDER}.env_key=SUPER_RELAY_KEY "
        f"--config model_providers.{MODEL_PROVIDER}.request_max_retries=0 "
        f"--config model_context_window={EXPECTED_WINDOW} "
        "--config model_instructions_file=/root/.codex/relay_base_instructions.txt "
        "--config model_catalog_json=/root/.codex/relay_model_catalog.json"
    )
    command = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--name",
        codex_container_name(instance_id, side),
        "--entrypoint",
        "sh",
        "-v",
        f"{CODEX_LINUX}:/opt/gsb-codex:ro",
        "-v",
        f"{work_dir}:{container_dir}",
        "-v",
        f"{codex_home}:/root/.codex",
        "-e",
        "SUPER_RELAY_KEY",
        "--add-host",
        f"{relay.CONTAINER_HOST}:host-gateway",
        "-w",
        container_dir,
        image,
        "-c",
        script,
    ]
    remove_codex_container(instance_id, side)
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        try:
            proc = subprocess.run(
                command,
                input=prompt.encode("utf-8"),
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=CODEX_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            remove_codex_container(instance_id, side)
            log.write(f"\nERROR: Codex timed out after {CODEX_TIMEOUT_SECONDS // 3600}h\n")
            return 124
    return proc.returncode


def last_log_error(log_path: Path) -> str:
    if not log_path.exists():
        return ""
    lines = [line.strip() for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    errors = [line for line in lines if "ERROR:" in line]
    if errors:
        return errors[-1]
    return lines[-1] if lines else ""


def parse_rollout(path: Path) -> tuple[str | None, str | None, int | None]:
    session_id = None
    cwd = None
    window = None
    with path.open(encoding="utf-8", errors="replace") as handle:
        for index, line in enumerate(handle):
            if index > 40 and session_id and cwd and window:
                break
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = row.get("payload") or {}
            if row.get("type") == "session_meta":
                session_id = payload.get("session_id")
                cwd = payload.get("cwd")
            if payload.get("model_context_window") is not None:
                window = payload.get("model_context_window")
    return session_id, cwd, window


def collect_rollout(instance_id: str, side: str, started: float, ended: float) -> dict:
    expected_cwd = f"/work/{instance_id}/{side}"
    sessions = side_codex_home(instance_id, side) / "sessions"
    matches: list[tuple[float, Path]] = []
    for path in sessions.rglob("rollout-*.jsonl"):
        modified = path.stat().st_mtime
        if modified < started - 15 or modified > ended + 90:
            continue
        session_id, cwd, window = parse_rollout(path)
        if cwd != expected_cwd:
            continue
        matches.append((modified, path, session_id, window))  # type: ignore[arg-type]
    if not matches:
        raise RuntimeError(f"no rollout for {expected_cwd}")
    matches.sort(key=lambda item: item[0])
    _modified, path, session_id, window = matches[-1]
    if window != EXPECTED_WINDOW:
        raise RuntimeError(f"model_context_window is {window}, expected {EXPECTED_WINDOW}")
    dest = RUNS / instance_id / "artifacts" / f"{side}-rollout.jsonl"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
    return {"session_id": session_id, "model_context_window": window, "rollout_file": str(dest)}


# GitHub rejects a push that contains a blob at or above this size.
_GITHUB_BLOB_LIMIT = 100 * 1024 * 1024


def _unstage_huge(repo: Path) -> None:
    staged = git(repo, "diff", "--cached", "--name-only", "-z")
    if staged.returncode != 0:
        raise RuntimeError(staged.stderr.strip() or "git diff --cached failed")
    huge: list[str] = []
    for path in staged.stdout.split("\0"):
        if not path:
            continue
        file = repo / path
        try:
            if file.is_file() and file.stat().st_size >= _GITHUB_BLOB_LIMIT:
                huge.append(path)
        except OSError:
            continue
    if not huge:
        return
    print(f"leave {len(huge)} files over 100MB out of the snapshot", flush=True)
    removed = git(repo, "rm", "--cached", "-f", "--ignore-unmatch", "--", *huge)
    if removed.returncode != 0:
        raise RuntimeError(removed.stderr.strip() or "git rm --cached failed")


def _huge_paths(repo: Path, base: str) -> list[str]:
    listed = git(repo, "rev-list", "--objects", f"{base}..HEAD")
    if listed.returncode != 0:
        raise RuntimeError(listed.stderr.strip() or "git rev-list failed")
    blobs: list[tuple[str, str]] = []
    for line in listed.stdout.splitlines():
        sha, _, path = line.partition(" ")
        if path:
            blobs.append((sha, path))
    if not blobs:
        return []
    checked = git(repo, "cat-file", "--batch-check=%(objecttype) %(objectsize)", input="\n".join(sha for sha, _path in blobs) + "\n")
    if checked.returncode != 0:
        raise RuntimeError(checked.stderr.strip() or "git cat-file failed")
    huge: list[str] = []
    seen: set[str] = set()
    for (_sha, path), info in zip(blobs, checked.stdout.splitlines()):
        kind, _, size = info.partition(" ")
        if kind != "blob" or not size.isdigit() or int(size) < _GITHUB_BLOB_LIMIT:
            continue
        if path not in seen:
            seen.add(path)
            huge.append(path)
    return huge


def _sh_quote(path: str) -> str:
    return "'" + path.replace("'", "'\\''") + "'"


def _drop_huge_history(repo: Path, base: str) -> None:
    """Rewrite commits after base so a push is not rejected for a blob over 100MB."""
    huge = _huge_paths(repo, base)
    if not huge:
        return
    print(f"drop {len(huge)} files over 100MB from history before push", flush=True)
    current = git(repo, "branch", "--show-current")
    if current.returncode != 0:
        raise RuntimeError(current.stderr.strip() or "git branch failed")
    if not current.stdout.strip():
        parked = git(repo, "checkout", "-B", "gsb-strip-large")
        if parked.returncode != 0:
            raise RuntimeError(parked.stderr.strip() or "git checkout failed")
    quoted = " ".join(_sh_quote(path) for path in huge)
    rewritten = git(
        repo,
        "filter-branch",
        "-f",
        "--index-filter",
        f"git rm -r --cached --ignore-unmatch -- {quoted}",
        f"{base}..HEAD",
    )
    if rewritten.returncode != 0:
        detail = (rewritten.stderr or rewritten.stdout).strip()
        raise RuntimeError(detail or "git filter-branch failed")


def capture_head(instance_id: str, side: str, base: str) -> dict:
    repo = WORK / instance_id / side
    clear_git_locks(repo)
    dirty = git(repo, "status", "--porcelain")
    if dirty.returncode != 0:
        raise RuntimeError(dirty.stderr.strip() or "git status failed")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    snapshot = False
    empty = False
    if dirty.stdout.strip():
        added = git(repo, "add", "-A")
        if added.returncode != 0:
            raise RuntimeError(added.stderr.strip() or "git add failed")
        _unstage_huge(repo)
        staged = git(repo, "diff", "--cached", "--quiet")
        if staged.returncode == 1:
            committed = git(repo, "commit", "--no-verify", "-m", "snapshot: workflow capture of uncommitted agent changes")
            if committed.returncode != 0:
                raise RuntimeError(committed.stderr.strip() or "snapshot commit failed")
            head = git(repo, "rev-parse", "HEAD").stdout.strip()
            snapshot = True
        elif staged.returncode != 0:
            raise RuntimeError(staged.stderr.strip() or "git diff --cached failed")
    if base and head and head != base:
        _drop_huge_history(repo, base)
        head = git(repo, "rev-parse", "HEAD").stdout.strip()
    if head == base:
        empty = True
        head = ""
    return {"head": head, "snapshot_commit": snapshot, "empty": empty}


def side_branch(instance_id: str, side: str) -> str:
    slug = instance_id.split("__", 1)[-1]
    return f"{slug}/{side.upper()}"


def push_side(instance_id: str, side: str, url: str) -> str:
    repo = WORK / instance_id / side
    branch = side_branch(instance_id, side)
    print(f"git push {branch}", flush=True)

    def push_once() -> None:
        pushed = git(repo, "push", "--force", url, f"HEAD:refs/heads/{branch}")
        if pushed.returncode != 0:
            detail = (pushed.stderr or pushed.stdout).strip()
            raise RuntimeError(detail or f"git push failed for {branch}")

    call_with_retry(push_once, f"git push {branch}", network_only=True)
    return branch


def diff_stats(instance_id: str, side: str, base: str, head: str) -> dict:
    if not head:
        return {"descendant": True, "files": [], "diff_lines": 0}
    repo = WORK / instance_id / side
    ancestor = git(repo, "merge-base", "--is-ancestor", base, head)
    names = git(repo, "diff", "--name-only", f"{base}..{head}")
    numstat = git(repo, "diff", "--numstat", f"{base}..{head}")
    files = [line for line in names.stdout.splitlines() if line.strip()]
    changed = 0
    for line in numstat.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        for value in parts[:2]:
            if value.isdigit():
                changed += int(value)
    return {"descendant": ancestor.returncode == 0, "files": files, "diff_lines": changed}


def test_container_name(instance_id: str, side: str) -> str:
    return f"gsb-test-{instance_id}-{side}"


def remove_test_container(instance_id: str, side: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", test_container_name(instance_id, side)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def run_test(instance_id: str, side: str, image: str, test_cmd: str, timeout: int) -> int | None:
    if not test_cmd:
        return None
    work_dir = WORK / instance_id / side
    container_dir = f"/work/{instance_id}/{side}"
    log_path = RUNS / instance_id / "logs" / f"{side}-test.log"
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        test_container_name(instance_id, side),
        "--entrypoint",
        "sh",
        "-v",
        f"{work_dir}:{container_dir}",
        "-w",
        container_dir,
        image,
        "-c",
        test_cmd,
    ]
    remove_test_container(instance_id, side)
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        try:
            proc = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            remove_test_container(instance_id, side)
            log.write(f"\nTIMEOUT after {timeout}s\n")
            return None
    return proc.returncode
