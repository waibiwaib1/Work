"""Export successful tasks that are not on the online sheet yet."""

from __future__ import annotations

import csv
import json
import sys

from paths import HARNESS_STATE, SHEET, SHEET_COLUMNS, snapshot_url
from records import load_state, load_task, task_outcome, task_type_from_categories, utc_now, write_json

EVALS = SHEET.parent / "evals.json"
_EVAL_COLUMNS = (
    "A-交付完整性（1-5）",
    "A-交付完整性描述",
    "B-交付完整性（1-5）",
    "B-交付完整性描述",
    "GSB 结论",
    "GSB 理由",
)

_MACHINE_COLUMNS = (
    "User Prompt",
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
    "B-SessionID",
    "B-轨迹文件",
    "B-产物快照",
    "B-运行录屏",
)


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
    sheet["提交人"] = "Alex Wang"
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


def load_eval_records() -> dict[str, dict]:
    if not EVALS.exists():
        return {}
    payload = json.loads(EVALS.read_text(encoding="utf-8"))
    records = payload.get("evals") or {}
    return {key: value for key, value in records.items() if isinstance(value, dict)}


def apply_eval(sheet: dict[str, str], record: dict | None) -> dict[str, str]:
    if not record:
        return sheet
    for column in _EVAL_COLUMNS:
        value = record.get(column)
        if value is None or value == "":
            continue
        sheet[column] = str(value)
    return sheet


def merge_sheet_row(existing: dict[str, str], fresh: dict[str, str], from_eval: bool) -> dict[str, str]:
    merged = dict(fresh)
    for column in SHEET_COLUMNS:
        if column == "instance_id" or column in _MACHINE_COLUMNS:
            continue
        if from_eval and column in _EVAL_COLUMNS and fresh.get(column):
            continue
        merged[column] = existing.get(column) or fresh.get(column) or ""
    return merged


def mark_uploaded(instance_ids: set[str]) -> int:
    state = load_state()
    tasks = state.get("tasks") or {}
    now = utc_now()
    marked = 0
    for instance_id in instance_ids:
        task = tasks.get(instance_id)
        if not task or task.get("uploaded"):
            continue
        task["uploaded"] = True
        task["uploaded_at"] = now
        marked += 1
    if not marked:
        return 0
    state["updated_at"] = now
    write_json(HARNESS_STATE, state)
    return marked


def export_pending_sheet() -> tuple[int, int, list[str]]:
    """Insert success tasks that are not on the online sheet yet.

    Machine columns come from task.json. Score columns come from evals.json
    when that task has a record. Other columns already filled in the csv stay.
    A task with an eval record is marked uploaded in status.json.
    """
    tasks = load_state().get("tasks") or {}
    evals = load_eval_records()
    notes: list[str] = []
    pending: list[tuple[str, str]] = []
    for instance_id, task in tasks.items():
        if task.get("uploaded"):
            continue
        outcome = task_outcome(task)
        if outcome != "success":
            notes.append(f"skip {instance_id}: {outcome or 'not finished'}")
            continue
        if not load_task(instance_id):
            notes.append(f"skip {instance_id}: no task.json")
            continue
        pending.append((str(task.get("started_at") or ""), instance_id))
    pending.sort()
    inserted = 0
    updated = 0
    if pending:
        rows = read_sheet_rows()
        index = {row.get("instance_id"): i for i, row in enumerate(rows)}
        for _, instance_id in pending:
            fresh = apply_eval(build_sheet_row({"instance_id": instance_id}), evals.get(instance_id))
            found = index.get(instance_id)
            if found is None:
                rows.append(fresh)
                index[instance_id] = len(rows) - 1
                inserted += 1
                continue
            rows[found] = merge_sheet_row(rows[found], fresh, instance_id in evals)
            updated += 1
        write_sheet_rows(rows)
    marked = mark_uploaded(set(evals) & set(tasks))
    if marked:
        notes.append(f"uploaded {marked}")
    return inserted, updated, notes


def main() -> int:
    try:
        inserted, updated, notes = export_pending_sheet()
    except PermissionError:
        print("sheet.csv is open; close it and run export again", file=sys.stderr)
        return 1
    for note in notes:
        print(note)
    print(f"inserted {inserted}  updated {updated}  {SHEET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
