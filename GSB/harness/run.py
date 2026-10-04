"""Run GSB pair tasks in task images and record mechanical results.

A and B of one task run at the same time. Different tasks can run at the
same time, up to --concurrency. Codex is the shared Linux install mounted
read-only from GSB/codex-linux. Each workspace is mounted at its own path so
parallel sessions can be told apart.

Human fields (task type, difficulty, scores, GSB) stay empty.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import relay
from paths import CODEX_LINUX, HARNESS_STATE, PUSH_URL, RUNS, snapshot_url
from records import (
    build_catalog,
    load_candidates,
    load_state,
    load_task,
    mark_running_interrupted,
    render_status,
    reset_tasks,
    store_side,
    task_outcome,
    task_record_path,
    update_status,
    utc_now,
    write_json,
)
from record_test import find_ffmpeg, record_side
from upload import upload_side
from runtime import (
    capture_head,
    collect_rollout,
    diff_stats,
    docker_pull,
    image_acquire,
    image_release,
    last_log_error,
    prepare,
    push_side,
    run_codex,
    run_test,
    side_branch,
)


def run_side(instance_id: str, side: str, image: str, prompt: str, base: str, push_url: str) -> dict:
    update_status(instance_id, phase="running", **{side: "running"})
    side_started = time.time()
    started_at = utc_now()
    log_path = RUNS / instance_id / "logs" / f"{side}.log"
    code = run_codex(instance_id, side, image, prompt, log_path)
    side_ended = time.time()
    if code != 0:
        detail = last_log_error(log_path)
        message = f"{side} codex exit {code}"
        if detail:
            message = f"{message}: {detail}"
        update_status(instance_id, **{side: "fail"})
        raise RuntimeError(message)
    try:
        rollout = collect_rollout(instance_id, side, side_started, side_ended)
        head_info = capture_head(instance_id, side, base)
        branch = push_side(instance_id, side, push_url) if push_url else side_branch(instance_id, side)
    except Exception:
        update_status(instance_id, **{side: "fail"})
        raise
    info = {
        "started_at": started_at,
        "ended_at": utc_now(),
        "exit_code": code,
        **rollout,
        **head_info,
        "branch": branch,
        "permalink": snapshot_url(head_info.get("head") or ""),
    }
    store_side(instance_id, side, info)
    update_status(instance_id, **{side: "done"})
    return info


def _artifact(instance_id: str, side: str, info: dict, key: str, name: str) -> Path:
    raw = info.get(key) or ""
    return Path(raw) if raw else RUNS / instance_id / "artifacts" / f"{side}-{name}"


def delivery_gap(instance_id: str, task: dict) -> str:
    """Finished sides that still need a test video or an S3 upload."""
    if any(task.get(side) not in ("done", "ok") for side in ("a", "b")):
        return ""
    record = load_task(instance_id)
    test_cmd = (record.get("test_cmd") or "").strip()
    sides = record.get("sides") or {}
    missing = []
    for side in ("a", "b"):
        info = sides.get(side) or {}
        video = _artifact(instance_id, side, info, "test_video", "test.mp4")
        video_ok = task.get(f"video_{side}") == "done" and video.is_file() and video.stat().st_size > 0
        if test_cmd and not video_ok:
            missing.append(f"video {side}")
        rollout = _artifact(instance_id, side, info, "rollout_file", "rollout.jsonl")
        if rollout.is_file() and not info.get("s3_rollout"):
            missing.append(f"s3 rollout {side}")
        if video_ok and not info.get("s3_url"):
            missing.append(f"s3 video {side}")
    return ", ".join(missing)


def execute_task(
    row: dict,
    meta_extra: dict,
    run_id: str,
    force: bool,
    retry: bool,
    run_tests: bool,
    test_timeout: int,
    push_url: str,
) -> None:
    instance_id = row["instance_id"]
    base = row["base_commit"]
    image = meta_extra["image_name"]
    started = time.time()
    previous = (load_state().get("tasks") or {}).get(instance_id, {})
    saved_sides = (load_task(instance_id).get("sides") or {})
    keep = set()
    if not force:
        for side in ("a", "b"):
            if previous.get(side) in ("done", "ok") and saved_sides.get(side):
                keep.add(side)
    preserved_start = previous.get("started_at") if keep == {"a", "b"} else None
    status_fields = {
        "phase": "preparing",
        "status": "running",
        "started_at": preserved_start or utc_now(),
        "ended_at": None,
        "duration_seconds": None,
        "uploaded": False,
        "uploaded_at": None,
        "run_id": run_id,
        "error": "",
        "a": "done" if "a" in keep else "prep",
        "b": "done" if "b" in keep else "prep",
    }
    for side in ("a", "b"):
        if side not in keep:
            status_fields[f"video_{side}"] = ""
            status_fields[f"s3_{side}"] = ""
    update_status(instance_id, **status_fields)
    acquired = False
    succeeded = False
    try:
        prompt = prepare(row, meta_extra, force, retry, keep)
        docker_pull(image)
        image_acquire(image)
        acquired = True
        sides: dict[str, dict] = {side: saved_sides[side] for side in keep}

        errors: list[BaseException] = []
        pending = [side for side in ("a", "b") if side not in keep]
        if pending:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {
                    pool.submit(run_side, instance_id, side, image, prompt, base, push_url): side for side in pending
                }
                for future in as_completed(futures):
                    side = futures[future]
                    try:
                        sides[side] = future.result()
                    except Exception as exc:  # noqa: BLE001 - the other side keeps running
                        errors.append(exc)
        if errors:
            raise RuntimeError("; ".join(str(exc) for exc in errors))
        summary_sides = {}
        ffmpeg = find_ffmpeg() if run_tests else None
        if run_tests and not ffmpeg:
            print(f"{instance_id} recording skipped: ffmpeg is not installed", flush=True)
        for side, info in sides.items():
            summary_sides[side] = diff_stats(instance_id, side, base, info["head"])
            if not run_tests:
                summary_sides[side]["test_exit"] = None
                continue
            recorded = False
            if ffmpeg:
                try:
                    code = record_side(instance_id, side, test_timeout, ffmpeg, False)
                except Exception as exc:  # noqa: BLE001 - recording must not discard the finished task
                    print(f"{instance_id} {side} recording failed: {exc}", flush=True)
                    code = 1
                saved = (load_task(instance_id).get("sides") or {}).get(side) or {}
                if code == 0 and saved.get("test_video"):
                    summary_sides[side]["test_exit"] = saved.get("test_exit")
                    summary_sides[side]["test_video"] = saved["test_video"]
                    if saved.get("s3_url"):
                        summary_sides[side]["s3_url"] = saved["s3_url"]
                    recorded = True
                elif code != 0:
                    print(f"{instance_id} {side} recording failed, running the test without video", flush=True)
            if not recorded:
                if side in keep and "test_exit" in info:
                    summary_sides[side]["test_exit"] = info.get("test_exit")
                else:
                    summary_sides[side]["test_exit"] = run_test(
                        instance_id, side, image, meta_extra.get("test_cmd") or "", test_timeout
                    )
        record = load_task(instance_id)
        record["sides"] = {}
        for side, info in sides.items():
            merged = {**info, **summary_sides[side]}
            if side not in keep and not merged.get("test_video"):
                merged.pop("test_video", None)
                merged.pop("s3_url", None)
            try:
                upload_side(instance_id, side, merged)
            except Exception as exc:  # noqa: BLE001 - a missing upload must not discard the run
                print(f"{instance_id} {side} upload failed: {exc}", flush=True)
            record["sides"][side] = merged
        write_json(task_record_path(instance_id), record)
        uploaded_sides = {
            f"s3_{side}": "done"
            for side, info in record["sides"].items()
            if info.get("s3_rollout") or info.get("s3_url")
        }
        duration = round(time.time() - started, 1)
        if preserved_start:
            try:
                original = datetime.strptime(preserved_start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                duration = round((datetime.now(timezone.utc) - original).total_seconds(), 1)
            except ValueError:
                pass
        update_status(
            instance_id,
            phase="done",
            status="success",
            ended_at=utc_now(),
            duration_seconds=duration,
            error="",
            **uploaded_sides,
        )
        succeeded = True
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
    finally:
        if acquired:
            image_release(image, remove=succeeded)


def cmd_run(args: argparse.Namespace) -> int:
    if not os.environ.get("SUPER_RELAY_KEY"):
        print("SUPER_RELAY_KEY is not set", file=sys.stderr)
        return 1
    codex_bin = CODEX_LINUX / "bin" / "codex"
    try:
        codex_bin.lstat()
    except OSError:
        print(f"missing {codex_bin}", file=sys.stderr)
        return 1
    only = set(args.instance) if args.instance else None
    run_id = uuid.uuid4().hex[:12]
    interrupted = mark_running_interrupted()
    if interrupted:
        print(f"marked {len(interrupted)} stale running tasks as interrupted", flush=True)
    rows = load_candidates(None, only)
    state = load_state()
    pending = []
    backfill = []
    skipped = 0
    for row in rows:
        instance_id = row["instance_id"]
        task = state.get("tasks", {}).get(instance_id, {})
        outcome = task_outcome(task)
        if outcome == "success" and not args.force:
            gap = delivery_gap(instance_id, task)
            if gap:
                row["_retry"] = False
                backfill.append(row)
                print(f"backfill {instance_id}: {gap}", flush=True)
            else:
                skipped += 1
            continue
        row["_retry"] = outcome in ("failed", "running")
        pending.append(row)
    if args.limit:
        pending = pending[: args.limit]
    pending = backfill + pending
    if not pending:
        print(f"nothing to run, skipped {skipped} success  status={HARNESS_STATE}")
        return 0
    print(f"run {len(pending)}, backfill {len(backfill)}, skipped {skipped} success", flush=True)
    relay.set_activity_path(RUNS / "_harness" / "relay.json")
    relay_server = relay.start_relay(slots=args.concurrency * 2)
    catalog = build_catalog({row["instance_id"] for row in pending})
    failures = 0
    try:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {
                pool.submit(
                    execute_task,
                    row,
                    catalog[row["instance_id"]],
                    run_id,
                    args.force,
                    row["_retry"],
                    args.test,
                    args.test_timeout,
                    args.push_url,
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
    finally:
        relay_server.shutdown()
        relay.clear_activity()
    print(f"finished {len(pending) - failures}/{len(pending)}  status={HARNESS_STATE}")
    return 1 if failures else 0


def cmd_reset(args: argparse.Namespace) -> int:
    reset, missing = reset_tasks(args.instance)
    for instance_id in reset:
        print(f"reset {instance_id}")
    for instance_id in missing:
        print(f"not in status: {instance_id}", file=sys.stderr)
    return 1 if missing or not reset else 0


def cmd_status(args: argparse.Namespace) -> int:
    if args.once:
        print(render_status(), end="")
        return 0
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint()
        if ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    print("\033[?1049h\033[?25l", end="", flush=True)
    try:
        while True:
            try:
                size = os.get_terminal_size(sys.stdout.fileno())
            except OSError:
                size = os.terminal_size((120, 30))
            lines = render_status().splitlines()
            available = max(size.lines - 1, 1)
            hidden = max(len(lines) - available, 0)
            lines = lines[:available]
            if hidden and available > 2:
                lines[-1] = f"  … {hidden} more rows"
            frame = "\n".join(line[: size.columns] for line in lines)
            print("\033[2J\033[H" + frame, end="", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        return 0
    finally:
        print("\033[?25h\033[?1049l", end="", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run and monitor GSB pair tasks")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="prepare, run A then B, collect rollouts")
    run_parser.add_argument("--limit", type=int, default=None, help="run at most N pending tasks")
    run_parser.add_argument("--instance", action="append", default=[], help="only consider these instance ids; repeatable")
    run_parser.add_argument("--concurrency", type=int, default=1, help="how many tasks at once; each task runs A and B together")
    run_parser.add_argument("--force", action="store_true", help="also rerun successes, and clone again")
    run_parser.add_argument("--test", action=argparse.BooleanOptionalAction, default=True, help="run test_cmd after each side and record the exit code; --no-test skips it")
    run_parser.add_argument("--test-timeout", type=int, default=1800, help="seconds for one test_cmd")
    run_parser.add_argument("--push-url", default=PUSH_URL, help="git remote that receives each side branch")
    run_parser.set_defaults(func=cmd_run)

    reset_parser = sub.add_parser("reset", help="mark tasks so the next run executes them again")
    reset_parser.add_argument("--instance", action="append", required=True, help="instance id to run again; repeatable")
    reset_parser.set_defaults(func=cmd_reset)

    status_parser = sub.add_parser("status", help="show tasks, refreshing every second")
    status_parser.add_argument("--once", action="store_true", help="print one frame and exit")
    status_parser.set_defaults(func=cmd_status)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
