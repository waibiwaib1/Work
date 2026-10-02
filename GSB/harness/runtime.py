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
from paths import CODEX_HOME, CODEX_LINUX, EXPECTED_WINDOW, MODEL, MODEL_PROVIDER, RUNS, WORK
from records import load_task, task_record_path, task_type_from_categories, write_json

_pull_lock = threading.Lock()
_pulled: set[str] = set()
_image_users: dict[str, int] = {}


def run_cmd(args: list[str], cwd: Path | None = None, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def run_visible(args: list[str]) -> None:
    print(" ".join(args), flush=True)
    completed = subprocess.run(args, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"command failed ({completed.returncode}): {' '.join(args)}")


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


def git(repo: Path, *args: str, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return run_cmd(["git", "-C", str(repo), *args], timeout=timeout)


def remove_tree(path: Path) -> None:
    def onerror(func, target, exc_info):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onerror=onerror)


def reset_to_base(repo: Path, sha: str, branch: str) -> None:
    hard = git(repo, "reset", "--hard", sha)
    if hard.returncode != 0:
        raise RuntimeError(hard.stderr.strip() or "git reset failed")
    cleaned = git(repo, "clean", "-fd")
    if cleaned.returncode != 0:
        raise RuntimeError(cleaned.stderr.strip() or "git clean failed")
    branched = git(repo, "checkout", "-B", branch, sha)
    if branched.returncode != 0:
        raise RuntimeError(branched.stderr.strip() or "git branch failed")


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
            run_visible(
                [
                    "git",
                    "-c",
                    "http.lowSpeedLimit=1000",
                    "-c",
                    "http.lowSpeedTime=60",
                    "clone",
                    "--progress",
                    f"https://github.com/{row['repo']}.git",
                    str(src),
                ]
            )

        call_with_retry(clone_once, "git clone")
        checked = git(src, "checkout", "--detach", sha)
        if checked.returncode != 0:
            raise RuntimeError(checked.stderr.strip() or "git checkout failed")
    for side in ("a", "b"):
        if side in keep:
            continue
        side_dir = WORK / instance_id / side
        branch = side_branch(instance_id, side)
        if force and side_dir.exists():
            remove_tree(side_dir)
        created = not side_dir.exists()
        if created:
            shutil.copytree(src, side_dir)
        if retry and not created:
            reset_to_base(side_dir, sha, branch)
        else:
            branched = git(side_dir, "checkout", "-B", branch, sha)
            if branched.returncode != 0:
                raise RuntimeError(branched.stderr.strip() or "git branch failed")
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


def run_codex(instance_id: str, side: str, image: str, prompt: str, log_path: Path) -> int:
    work_dir = WORK / instance_id / side
    container_dir = f"/work/{instance_id}/{side}"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "export PATH=/opt/gsb-codex/bin:$PATH; "
        "exec codex exec --dangerously-bypass-approvals-and-sandbox "
        f"--config model_provider={MODEL_PROVIDER} --model {MODEL} "
        f"--config model_providers.super_relay.base_url={relay.proxy_base(instance_id, side)} "
        "--config model_instructions_file=/root/.codex/relay_base_instructions.txt "
        "--config model_catalog_json=/root/.codex/relay_model_catalog.json"
    )
    command = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--entrypoint",
        "sh",
        "-v",
        f"{CODEX_LINUX}:/opt/gsb-codex:ro",
        "-v",
        f"{work_dir}:{container_dir}",
        "-v",
        f"{CODEX_HOME}:/root/.codex",
        "-e",
        "SUPER_RELAY_KEY",
        "--add-host",
        "host.docker.internal:host-gateway",
        "-w",
        container_dir,
        image,
        "-c",
        script,
    ]
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.run(
            command,
            input=prompt.encode("utf-8"),
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
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
    sessions = CODEX_HOME / "sessions"
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


def capture_head(instance_id: str, side: str, base: str) -> dict:
    repo = WORK / instance_id / side
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
        committed = git(repo, "commit", "-m", "snapshot: workflow capture of uncommitted agent changes")
        if committed.returncode != 0:
            raise RuntimeError(committed.stderr.strip() or "snapshot commit failed")
        head = git(repo, "rev-parse", "HEAD").stdout.strip()
        snapshot = True
    elif head == base:
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
