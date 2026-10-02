"""Run GSB pair tasks in task images and record mechanical results.

A and B of one task run one after the other. Different tasks can run at the
same time, up to --concurrency. Codex is the shared Linux install mounted
read-only from GSB/codex-linux. Each workspace is mounted at its own path so
parallel sessions can be told apart.

Human fields (task type, difficulty, scores, GSB) stay empty.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(r"D:\Code\Work\GSB")
CANDIDATES = ROOT / "candidates-3000.jsonl"
SOURCE = Path(r"D:\Code\Work\Multi-SWE-bench\SWE-rebench\SWE-rebench")
CODEX_LINUX = ROOT / "codex-linux"
CODEX_HOME = Path.home() / ".codex"
RUNS = Path(r"D:\Code\GSB\runs")
WORK = Path(r"D:\Code\Work\GSB\task")
HARNESS_STATE = RUNS / "_harness" / "status.json"
CATALOG = RUNS / "_harness" / "catalog.json"
SHEET = RUNS / "_harness" / "sheet.csv"
SHEET_COLUMNS = [
    "instance_id",
    "User Prompt",
    "提交人",
    "任务类型",
    "Harness",
    "任务难度",
    "语言框架",
    "操作系统",
    "环境可复现等级",
    "初始环境快照",
    "A-SessionID",
    "A-轨迹文件",
    "A-产物快照",
    "A-运行录屏",
    "A-交付完整性（1-5）",
    "A-交付完整性描述",
    "B-SessionID",
    "B-轨迹文件",
    "B-产物快照",
    "B-运行录屏",
    "B-交付完整性（1-5）",
    "B-交付完整性描述",
    "GSB 结论",
    "GSB 理由",
    "内部质检",
    "质检反馈",
    "备注",
    "固定工时（分钟）",
    "提交时间",
    "修改时间",
    "检查",
    "排异",
]

MODEL_PROVIDER = "super_relay"
MODEL = "auto_model/urm"
EXPECTED_WINDOW = 1000000

_state_lock = threading.Lock()
_sheet_lock = threading.Lock()
_pull_lock = threading.Lock()
_pulled: set[str] = set()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def git(repo: Path, *args: str, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return run_cmd(["git", "-C", str(repo), *args], timeout=timeout)


def load_candidates(limit: int | None, only: set[str] | None) -> list[dict]:
    rows = []
    with CANDIDATES.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if only and row["instance_id"] not in only:
                continue
            sha = row.get("base_commit") or ""
            prompt = (row.get("problem_statement") or "").strip()
            if len(sha) != 40 or not prompt:
                raise SystemExit(f"bad candidate: {row.get('instance_id')}")
            rows.append(row)
            if limit and len(rows) >= limit:
                break
    return rows


def build_catalog(wanted: set[str]) -> dict[str, dict]:
    if CATALOG.exists():
        cached = json.loads(CATALOG.read_text(encoding="utf-8"))
        if wanted <= set(cached):
            return {key: cached[key] for key in wanted}
    found: dict[str, dict] = {}
    if CATALOG.exists():
        found.update(json.loads(CATALOG.read_text(encoding="utf-8")))
    for path in SOURCE.rglob("*.jsonl"):
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"instance_id"' not in line:
                    continue
                marker = line.find('"instance_id": "')
                if marker < 0:
                    continue
                end = line.find('"', marker + 16)
                instance_id = line[marker + 16 : end]
                if instance_id not in wanted or instance_id in found and found[instance_id].get("image_name"):
                    continue
                row = json.loads(line)
                if row.get("instance_id") != instance_id:
                    continue
                install = row.get("install_config") or {}
                found[instance_id] = {
                    "image_name": row.get("image_name") or "",
                    "test_cmd": install.get("test_cmd") or "",
                    "language": row.get("language") or "",
                }
        if wanted <= set(found) and all(found[item].get("image_name") for item in wanted):
            break
    CATALOG.parent.mkdir(parents=True, exist_ok=True)
    CATALOG.write_text(json.dumps(found, ensure_ascii=False, indent=2), encoding="utf-8")
    missing = [item for item in wanted if not found.get(item, {}).get("image_name")]
    if missing:
        raise SystemExit("missing image_name for: " + ", ".join(missing[:10]))
    return {key: found[key] for key in wanted}


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_state() -> dict:
    if not HARNESS_STATE.exists():
        return {"updated_at": utc_now(), "tasks": {}}
    return json.loads(HARNESS_STATE.read_text(encoding="utf-8"))


def task_outcome(task: dict) -> str:
    status = task.get("status")
    if status in ("success", "failed", "running"):
        return status
    if task.get("phase") == "done":
        return "success"
    if task.get("phase") == "failed":
        return "failed"
    if task:
        return "running"
    return ""


def update_status(instance_id: str, **fields: object) -> None:
    with _state_lock:
        state = {"updated_at": utc_now(), "tasks": {}}
        if HARNESS_STATE.exists():
            state = json.loads(HARNESS_STATE.read_text(encoding="utf-8"))
        task = state.setdefault("tasks", {}).setdefault(instance_id, {})
        task.update(fields)
        state["updated_at"] = utc_now()
        write_json(HARNESS_STATE, state)
        phase = fields.get("phase", task.get("phase", ""))
        detail = fields.get("error") or ""
        print(f"[{utc_now()}] {instance_id} {phase} {detail}".rstrip(), flush=True)


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


def task_record_path(instance_id: str) -> Path:
    return RUNS / instance_id / "task.json"


def load_task(instance_id: str) -> dict:
    path = task_record_path(instance_id)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def prepare(row: dict, meta_extra: dict, force: bool, retry: bool) -> str:
    instance_id = row["instance_id"]
    sha = row["base_commit"]
    prompt = row["problem_statement"]
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
            "task_type": None,
            "difficulty": None,
            "repro_level": None,
            "initial_permalink": None,
            "delivery_a": None,
            "delivery_b": None,
            "gsb": None,
            "gsb_reason": None,
            "sides": {},
        },
    )
    src = WORK / instance_id / "src"
    if force and src.exists():
        shutil.rmtree(src)
    if not src.exists():
        src.parent.mkdir(parents=True, exist_ok=True)
        cloned = run_cmd(["git", "clone", f"https://github.com/{row['repo']}.git", str(src)])
        if cloned.returncode != 0:
            raise RuntimeError(cloned.stderr.strip() or "git clone failed")
        checked = git(src, "checkout", "--detach", sha)
        if checked.returncode != 0:
            raise RuntimeError(checked.stderr.strip() or "git checkout failed")
    for side in ("a", "b"):
        side_dir = WORK / instance_id / side
        branch = f"gsb/{instance_id}/{side}"
        if force and side_dir.exists():
            shutil.rmtree(side_dir)
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
        pulled = run_cmd(["docker", "pull", image])
        if pulled.returncode != 0:
            raise RuntimeError(pulled.stderr.strip() or f"docker pull failed: {image}")
        _pulled.add(image)


def run_codex(instance_id: str, side: str, image: str, prompt: str, log_path: Path) -> int:
    work_dir = WORK / instance_id / side
    container_dir = f"/work/{instance_id}/{side}"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "export PATH=/opt/gsb-codex/bin:$PATH; "
        "exec codex exec --dangerously-bypass-approvals-and-sandbox "
        f"--config model_provider={MODEL_PROVIDER} --model {MODEL}"
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
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        try:
            proc = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            log.write(f"\nTIMEOUT after {timeout}s\n")
            return None
    return proc.returncode


def execute_task(row: dict, meta_extra: dict, force: bool, retry: bool, run_tests: bool, test_timeout: int) -> None:
    instance_id = row["instance_id"]
    base = row["base_commit"]
    image = meta_extra["image_name"]
    started = time.time()
    update_status(
        instance_id,
        phase="preparing",
        status="running",
        started_at=utc_now(),
        ended_at=None,
        duration_seconds=None,
        uploaded=False,
        uploaded_at=None,
        error="",
    )
    upsert_sheet_row(build_sheet_row(row))
    try:
        prompt = prepare(row, meta_extra, force, retry)
        docker_pull(image)
        sides: dict[str, dict] = {}
        for side in ("a", "b"):
            update_status(instance_id, phase=f"running-{side}")
            side_started = time.time()
            started_at = utc_now()
            code = run_codex(instance_id, side, image, prompt, RUNS / instance_id / "logs" / f"{side}.log")
            side_ended = time.time()
            if code != 0:
                raise RuntimeError(f"{side} codex exit {code}")
            rollout = collect_rollout(instance_id, side, side_started, side_ended)
            head_info = capture_head(instance_id, side, base)
            sides[side] = {
                "started_at": started_at,
                "ended_at": utc_now(),
                "exit_code": code,
                **rollout,
                **head_info,
                "permalink": None,
            }
        summary_sides = {}
        for side, info in sides.items():
            summary_sides[side] = diff_stats(instance_id, side, base, info["head"])
            if run_tests:
                summary_sides[side]["test_exit"] = run_test(
                    instance_id, side, image, meta_extra.get("test_cmd") or "", test_timeout
                )
            else:
                summary_sides[side]["test_exit"] = None
        record = load_task(instance_id)
        record["sides"] = {
            side: {**info, **summary_sides[side]}
            for side, info in sides.items()
        }
        write_json(task_record_path(instance_id), record)
        upsert_sheet_row(build_sheet_row(row))
        update_status(
            instance_id,
            phase="done",
            status="success",
            ended_at=utc_now(),
            duration_seconds=round(time.time() - started, 1),
            error="",
        )
    except Exception as exc:
        update_status(
            instance_id,
            phase="failed",
            status="failed",
            ended_at=utc_now(),
            duration_seconds=round(time.time() - started, 1),
            error=str(exc),
        )
        raise


def cmd_run(args: argparse.Namespace) -> int:
    if not os.environ.get("SUPER_RELAY_KEY"):
        print("SUPER_RELAY_KEY is not set", file=sys.stderr)
        return 1
    if not (CODEX_LINUX / "bin" / "codex").exists():
        print(f"missing {CODEX_LINUX / 'bin' / 'codex'}", file=sys.stderr)
        return 1
    only = set(args.instance) if args.instance else None
    rows = load_candidates(None, only)
    state = load_state()
    pending = []
    skipped = 0
    for row in rows:
        outcome = task_outcome(state.get("tasks", {}).get(row["instance_id"], {}))
        if outcome == "success" and not args.force:
            skipped += 1
            continue
        row["_retry"] = outcome in ("failed", "running")
        pending.append(row)
    if args.limit:
        pending = pending[: args.limit]
    if not pending:
        print(f"nothing to run, skipped {skipped} success  status={HARNESS_STATE}")
        return 0
    print(f"run {len(pending)}, skipped {skipped} success", flush=True)
    catalog = build_catalog({row["instance_id"] for row in pending})
    failures = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(
                execute_task,
                row,
                catalog[row["instance_id"]],
                args.force,
                row["_retry"],
                args.test,
                args.test_timeout,
            ): row["instance_id"]
            for row in pending
        }
        for future in as_completed(futures):
            instance_id = futures[future]
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 - one task must not kill the pool
                failures += 1
                print(f"[{utc_now()}] {instance_id} failed {exc}", flush=True)
    print(f"finished {len(pending) - failures}/{len(pending)}  status={HARNESS_STATE}")
    return 1 if failures else 0


def blank_sheet_row() -> dict[str, str]:
    return {column: "" for column in SHEET_COLUMNS}


def read_sheet_rows() -> list[dict[str, str]]:
    if not SHEET.exists():
        return []
    with SHEET.open(encoding="utf-8-sig", newline="") as handle:
        return [{column: (row.get(column) or "") for column in SHEET_COLUMNS} for row in csv.DictReader(handle)]


def write_sheet_rows(rows: list[dict[str, str]]) -> None:
    SHEET.parent.mkdir(parents=True, exist_ok=True)
    with SHEET.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SHEET_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def build_sheet_row(row: dict) -> dict[str, str]:
    instance_id = row["instance_id"]
    task = load_task(instance_id)
    sheet = blank_sheet_row()
    sheet["instance_id"] = instance_id
    sheet["User Prompt"] = task.get("problem_statement") or row.get("problem_statement") or ""
    sheet["任务类型"] = task.get("task_type") or ""
    sheet["任务难度"] = task.get("difficulty") or ""
    sheet["语言框架"] = task.get("language") or row.get("language") or ""
    sheet["环境可复现等级"] = task.get("repro_level") or ""
    sheet["Harness"] = "Codex CLI"
    sheet["初始环境快照"] = task.get("base_commit") or row.get("base_commit") or ""
    for side, prefix in (("a", "A"), ("b", "B")):
        info = (task.get("sides") or {}).get(side) or {}
        sheet[f"{prefix}-SessionID"] = info.get("session_id") or ""
        sheet[f"{prefix}-轨迹文件"] = info.get("rollout_file") or ""
        sheet[f"{prefix}-产物快照"] = info.get("head") or ""
    sheet["A-交付完整性（1-5）"] = "" if task.get("delivery_a") is None else str(task.get("delivery_a"))
    sheet["B-交付完整性（1-5）"] = "" if task.get("delivery_b") is None else str(task.get("delivery_b"))
    sheet["GSB 结论"] = task.get("gsb") or ""
    sheet["GSB 理由"] = task.get("gsb_reason") or ""
    return sheet


def upsert_sheet_row(sheet: dict[str, str]) -> None:
    with _sheet_lock:
        rows = read_sheet_rows()
        for index, existing in enumerate(rows):
            if existing.get("instance_id") == sheet["instance_id"]:
                rows[index] = sheet
                break
        else:
            rows.append(sheet)
        write_sheet_rows(rows)


def cmd_status(_args: argparse.Namespace) -> int:
    if not HARNESS_STATE.exists():
        print("no runs yet")
        return 0
    state = json.loads(HARNESS_STATE.read_text(encoding="utf-8"))
    print(f"updated {state.get('updated_at')}")
    for instance_id, task in state.get("tasks", {}).items():
        outcome = task_outcome(task)
        duration = task.get("duration_seconds")
        spent = f"{duration}s" if duration is not None else "-"
        uploaded = "uploaded" if task.get("uploaded") else "pending"
        print(
            f"{outcome or task.get('phase', ''):<10} {spent:<8} {uploaded:<10} {task.get('started_at', '')}  {instance_id}  {task.get('error', '')}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run and monitor GSB pair tasks")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="prepare, run A then B, collect rollouts")
    run_parser.add_argument("--limit", type=int, default=None, help="run at most N pending tasks")
    run_parser.add_argument("--instance", action="append", default=[], help="only consider these instance ids; repeatable")
    run_parser.add_argument("--concurrency", type=int, default=1, help="how many tasks at once; A/B stay serial")
    run_parser.add_argument("--force", action="store_true", help="also rerun successes, and clone again")
    run_parser.add_argument("--test", action="store_true", help="run test_cmd in the task image after each side")
    run_parser.add_argument("--test-timeout", type=int, default=1800, help="seconds for one test_cmd")
    run_parser.set_defaults(func=cmd_run)

    status_parser = sub.add_parser("status", help="show task phases")
    status_parser.set_defaults(func=cmd_status)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
