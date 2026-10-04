"""Status ledger, task records, and the status table."""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, timezone

from paths import CANDIDATES, CATALOG, HARNESS_STATE, RUNS, SOURCE

_state_lock = threading.Lock()
_task_lock = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
        sides = " ".join(f"{key}={task[key]}" for key in ("a", "b") if task.get(key))
        print(f"[{utc_now()}] {instance_id} {phase} {sides} {detail}".rstrip(), flush=True)


def mark_running_interrupted() -> list[str]:
    """Close stale running entries from a previous harness process."""
    with _state_lock:
        state = load_state()
        interrupted = []
        now = datetime.now(timezone.utc)
        for instance_id, task in (state.get("tasks") or {}).items():
            if task_outcome(task) != "running":
                continue
            interrupted.append(instance_id)
            started = task.get("started_at")
            duration = None
            if started:
                try:
                    began = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    duration = round((now - began).total_seconds(), 1)
                except ValueError:
                    pass
            for side in ("a", "b"):
                if task.get(side) not in ("done", "ok"):
                    task[side] = "fail"
            task.update(
                phase="interrupted",
                status="failed",
                ended_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                duration_seconds=duration,
                error="previous run was interrupted",
            )
        if interrupted:
            state["updated_at"] = utc_now()
            write_json(HARNESS_STATE, state)
        return interrupted


def task_record_path(instance_id: str):
    return RUNS / instance_id / "task.json"


def load_task(instance_id: str) -> dict:
    path = task_record_path(instance_id)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def store_side(instance_id: str, side: str, info: dict) -> None:
    with _task_lock:
        record = load_task(instance_id)
        record.setdefault("sides", {})[side] = info
        write_json(task_record_path(instance_id), record)


def reset_tasks(instance_ids: list[str]) -> tuple[list[str], list[str]]:
    state = load_state()
    tasks = state.setdefault("tasks", {})
    missing = [instance_id for instance_id in instance_ids if instance_id not in tasks]
    reset = [instance_id for instance_id in instance_ids if instance_id in tasks]
    if reset:
        with _state_lock:
            state = load_state()
            for instance_id in reset:
                task = state.setdefault("tasks", {}).setdefault(instance_id, {})
                task.update(
                    phase="",
                    status="failed",
                    ended_at=None,
                    duration_seconds=None,
                    error="",
                    a="fail",
                    b="fail",
                )
                record = load_task(instance_id)
                if record:
                    record["sides"] = {}
                    write_json(task_record_path(instance_id), record)
            state["updated_at"] = utc_now()
            write_json(HARNESS_STATE, state)
    return reset, missing


def task_type_from_categories(categories: list[str] | None) -> str:
    names = " ".join(categories or [])
    if "bug" in names:
        return "Bug修复"
    if "feat" in names:
        return "feature迭代"
    return ""


def display_width(text: str) -> int:
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(width - display_width(text), 0)


def format_elapsed(seconds: float) -> str:
    total = max(int(seconds), 0)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{secs:02d}s"


CHINA = timezone(timedelta(hours=8))


def task_started(task: dict) -> str:
    started = task.get("started_at")
    if not started:
        return "-"
    try:
        start = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return "-"
    return start.astimezone(CHINA).strftime("%m-%d %H:%M:%S")


def task_elapsed(task: dict) -> str:
    if task.get("status") != "running" and task.get("duration_seconds") is not None:
        return format_elapsed(float(task["duration_seconds"]))
    started = task.get("started_at")
    if not started:
        return "-"
    try:
        start = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return "-"
    return format_elapsed((datetime.now(timezone.utc) - start).total_seconds())


def retry_elapsed(live: dict | None) -> str:
    starts = []
    for info in (live or {}).values():
        started = info.get("retry_since")
        if isinstance(started, (int, float)):
            starts.append(float(started))
    if not starts:
        return "-"
    return format_elapsed(datetime.now(timezone.utc).timestamp() - min(starts))


def load_activity() -> dict:
    path = RUNS / "_harness" / "relay.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def side_label(task: dict, side: str, live: dict | None = None) -> str:
    value = task.get(side)
    stored = {"ok": "done", "run": "running", "wait": "pending"}.get(value, value)
    if stored == "pending" and task.get("phase") == "preparing":
        stored = "prep"
    stage = stored if stored in ("prep", "pending", "running", "done", "fail") else ""
    if not stage and task.get("status") == "success":
        stage = "done"
    elif not stage and task.get("status") == "running":
        phase = str(task.get("phase") or "")
        if phase == "preparing":
            stage = "prep"
        elif phase == "running-a" and side == "b":
            stage = "pending"
        elif phase == "running-b" and side == "a":
            stage = "done"
        elif phase.startswith("running"):
            stage = "running"
    elif not stage and task.get("status") == "failed":
        stage = "fail"
    if not stage:
        stage = "-"
    info = (live or {}).get(side) or {}
    if stage == "running" and info.get("phase") == "retrying":
        count = info.get("count")
        if isinstance(count, int) and count > 0:
            return f"retrying {count}"
        return "retrying"
    return stage


def short_error(task: dict) -> str:
    if task.get("status") != "failed":
        return ""
    text = " ".join(str(task.get("error") or "").split())
    if not text:
        return ""
    return "; ".join(_short_error_part(part.strip()) for part in text.split(";") if part.strip())


def _short_error_part(text: str) -> str:
    match = re.match(r"command failed \((\d+)\): (.+)$", text)
    if match:
        code, rest = match.group(1), match.group(2)
        tokens = rest.split()
        if tokens and tokens[0] == "git":
            index = 1
            while index < len(tokens) and tokens[index] == "-c":
                index += 2
            sub = tokens[index] if index < len(tokens) else "git"
            return f"git {sub} failed ({code})"
        name = tokens[0] if tokens else "command"
        return f"{name} failed ({code})"
    if len(text) > 80:
        return text[:79] + "…"
    return text


def render_status() -> str:
    if not HARNESS_STATE.exists():
        return "no runs yet\n"
    try:
        state = json.loads(HARNESS_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "reading status...\n"
    tasks = state.get("tasks") or {}
    activity = load_activity()
    running = []
    failed = []
    success = 0
    pending_upload = 0
    for instance_id, task in tasks.items():
        outcome = task_outcome(task)
        if outcome == "running":
            running.append((instance_id, task))
        elif outcome == "failed":
            failed.append((instance_id, task))
        elif outcome == "success":
            success += 1
            if not task.get("uploaded"):
                pending_upload += 1
    running.sort(key=lambda item: (item[1].get("started_at") or "", item[0]), reverse=True)
    failed.sort(key=lambda item: (item[1].get("started_at") or "", item[0]), reverse=True)
    shown = running + failed
    rendered = [
        (
            instance_id,
            side_label(task, "a", activity.get(instance_id)),
            side_label(task, "b", activity.get(instance_id)),
            task_started(task),
            task_elapsed(task),
            retry_elapsed(activity.get(instance_id)),
            short_error(task),
        )
        for instance_id, task in shown
    ]
    id_width = max([4, *(len(item[0]) for item in rendered)], default=4)
    a_width = max([1, *(len(item[1]) for item in rendered)], default=1)
    b_width = max([1, *(len(item[2]) for item in rendered)], default=1)
    start_width = max([5, *(len(item[3]) for item in rendered)], default=5)
    total_width = max([5, *(len(item[4]) for item in rendered)], default=5)
    retry_width = max([5, *(len(item[5]) for item in rendered)], default=5)
    header = (
        f"  running {len(running)}  success {success}  failed {len(failed)}  pending upload {pending_upload}\n"
        f"  {pad('task', id_width)}  {pad('A', a_width)}  {pad('B', b_width)}"
        f"  {pad('start', start_width)}  {pad('total', total_width)}  {pad('retry', retry_width)}  error\n"
    )
    if not tasks:
        return header + "  no tasks\n"
    if not rendered:
        return header + "  no running tasks\n"
    lines = [
        f"  {pad(instance_id, id_width)}  {pad(side_a, a_width)}  {pad(side_b, b_width)}"
        f"  {pad(started, start_width)}  {pad(elapsed, total_width)}  {pad(retry, retry_width)}  {error}".rstrip()
        for instance_id, side_a, side_b, started, elapsed, retry, error in rendered
    ]
    return header + "\n".join(lines) + "\n"
