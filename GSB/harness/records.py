"""Status ledger, task records, the sheet, and the status table."""

from __future__ import annotations

import csv
import json
import re
import threading
from datetime import datetime, timezone

from paths import CANDIDATES, CATALOG, HARNESS_STATE, RUNS, SHEET, SHEET_COLUMNS, SOURCE, snapshot_url

_state_lock = threading.Lock()
_sheet_lock = threading.Lock()
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
    sheet["任务类型"] = task.get("task_type") or task_type_from_categories(row.get("pr_categories"))
    sheet["任务难度"] = task.get("difficulty") or "困难"
    sheet["语言框架"] = task.get("language") or row.get("language") or ""
    sheet["操作系统"] = task.get("os") or "Windows"
    sheet["环境可复现等级"] = task.get("repro_level") or "已容器化，可一键起环境"
    sheet["Harness"] = "Codex CLI"
    sheet["初始环境快照"] = task.get("base_commit") or row.get("base_commit") or ""
    for side, prefix in (("a", "A"), ("b", "B")):
        info = (task.get("sides") or {}).get(side) or {}
        sheet[f"{prefix}-SessionID"] = info.get("session_id") or ""
        sheet[f"{prefix}-轨迹文件"] = info.get("s3_rollout") or info.get("rollout_file") or ""
        sheet[f"{prefix}-产物快照"] = info.get("permalink") or snapshot_url(info.get("head") or "")
        sheet[f"{prefix}-运行录屏"] = info.get("s3_url") or ""
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


def task_label(task: dict) -> str:
    if task.get("status") == "running":
        phase = str(task.get("phase") or "running")
        if phase == "preparing":
            return "prep"
        if phase.startswith("running"):
            return "run"
        return phase
    return {"success": "ok", "failed": "fail"}.get(task_outcome(task), task_outcome(task) or "-")


def load_activity() -> dict:
    path = RUNS / "_harness" / "relay.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
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
    except json.JSONDecodeError:
        return "reading status...\n"
    tasks = state.get("tasks") or {}
    activity = load_activity()
    order = {"run": 0, "prep": 1, "fail": 2, "ok": 3}
    rows = sorted(tasks.items(), key=lambda item: (order.get(task_label(item[1]), 9), item[0]))
    id_width = max([4, *(len(instance_id) for instance_id in tasks)], default=4)
    rendered = []
    for instance_id, task in rows:
        rendered.append(
            (
                instance_id,
                side_label(task, "a", activity.get(instance_id)),
                side_label(task, "b", activity.get(instance_id)),
                task_elapsed(task),
                short_error(task),
            )
        )
    a_width = max([1, *(len(item[1]) for item in rendered)], default=1)
    b_width = max([1, *(len(item[2]) for item in rendered)], default=1)
    time_width = max([4, *(len(item[3]) for item in rendered)], default=4)
    header = f"  {pad('task', id_width)}  {pad('A', a_width)}  {pad('B', b_width)}  {pad('time', time_width)}  error\n"
    if not rendered:
        return header + "  no tasks\n"
    lines = [
        f"  {pad(instance_id, id_width)}  {pad(side_a, a_width)}  {pad(side_b, b_width)}  {pad(elapsed, time_width)}  {error}".rstrip()
        for instance_id, side_a, side_b, elapsed, error in rendered
    ]
    return header + "\n".join(lines) + "\n"
