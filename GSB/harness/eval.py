"""Score successful tasks: download each diff, ask a Cursor agent, delete the diff.

Same task filter as export.py: status success, not uploaded, task.json present.
Scores already stored in runs/_harness/evals.json are left as they are unless
--force is set. The agent only returns the eight fields. This script writes JSON.
"""

from __future__ import annotations

import argparse
import codecs
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from paths import PUSH_URL, RUNS, WORK
from records import load_state, load_task, task_outcome, utc_now, write_json

EVALS = RUNS / "_harness" / "evals.json"
DIFF_ROOT = WORK / "_eval"
SKILL = Path(__file__).resolve().parents[2] / ".cursor" / "skills" / "swe-bench-evaluator" / "SKILL.md"
REPO = PUSH_URL.removesuffix(".git")
FIELDS = (
    "A-测试结论",
    "A-交付完整性（1-5）",
    "A-交付完整性描述",
    "B-测试结论",
    "B-交付完整性（1-5）",
    "B-交付完整性描述",
    "GSB 结论",
    "GSB 理由",
)
SCORE_FIELDS = {"A-交付完整性（1-5）", "B-交付完整性（1-5）"}
VERDICTS = {"A 更好", "Same", "B 更好"}
READ_TOOLS = ("read", "grep", "glob", "ls")


def load_evals() -> dict:
    if not EVALS.exists():
        return {"updated_at": None, "evals": {}}
    payload = json.loads(EVALS.read_text(encoding="utf-8"))
    payload.setdefault("evals", {})
    return payload


def save_record(record: dict) -> None:
    payload = load_evals()
    payload["updated_at"] = utc_now()
    payload["evals"][record["instance_id"]] = record
    write_json(EVALS, payload)


def pending_ids(only: set[str], force: bool) -> list[str]:
    done = set() if force else set((load_evals().get("evals") or {}))
    pending: list[tuple[str, str]] = []
    for instance_id, task in (load_state().get("tasks") or {}).items():
        if only and instance_id not in only:
            continue
        if task.get("uploaded"):
            continue
        if task_outcome(task) != "success":
            continue
        if not load_task(instance_id):
            continue
        if instance_id in done:
            continue
        pending.append((str(task.get("started_at") or ""), instance_id))
    pending.sort()
    return [instance_id for _, instance_id in pending]


def side_spec(task: dict, side: str) -> dict:
    info = (task.get("sides") or {}).get(side) or {}
    head = str(info.get("head") or "").strip()
    base = str(task.get("base_commit") or "").strip()
    empty = bool(info.get("empty")) or not head or head == base
    return {
        "empty": empty,
        "head": "" if empty else head,
        "test_exit": info.get("test_exit"),
        "diff": None if empty else f"{side}.diff",
    }


def download(url: str, dest: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "gsb-eval"})
    last_error = "download failed"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                body = response.read()
            break
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code} for {url}"
            if exc.code not in (429, 502, 503, 504) or attempt == 2:
                raise RuntimeError(last_error) from exc
        except urllib.error.URLError as exc:
            last_error = str(exc.reason)
            if attempt == 2:
                raise RuntimeError(f"{url}: {last_error}") from exc
        time.sleep(min(30, 2 ** attempt))
    else:
        raise RuntimeError(last_error)
    stripped = body.lstrip()
    if stripped.startswith(b"<") or stripped.startswith(b"{"):
        raise RuntimeError(f"{url} did not return a unified diff")
    if stripped and not stripped.startswith(b"diff --git") and not stripped.startswith(b"--- "):
        raise RuntimeError(f"{url} did not return a unified diff")
    dest.write_bytes(body)


def prepare_diffs(instance_id: str) -> Path:
    task = load_task(instance_id)
    folder = DIFF_ROOT / instance_id
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    base = str(task.get("base_commit") or "").strip()
    manifest = {"instance_id": instance_id, "base_commit": base}
    for side in ("a", "b"):
        spec = side_spec(task, side)
        manifest[side] = spec
        if spec["empty"]:
            continue
        download(f"{REPO}/compare/{base}...{spec['head']}.diff", folder / f"{side}.diff")
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return folder


def remove_diffs(folder: Path) -> None:
    if folder.exists():
        shutil.rmtree(folder)


def skill_text() -> str:
    if not SKILL.is_file():
        raise SystemExit(f"missing skill: {SKILL}")
    return SKILL.read_text(encoding="utf-8")


def build_prompt(instance_id: str, folder: Path) -> str:
    task = load_task(instance_id)
    logs = RUNS / instance_id / "logs"
    return f"""{skill_text()}

按上面的 skill 评这一题，只评这一题。脚本会保存 JSON 并删除 diff。不要写 evals.json，不要改任何文件。

instance_id: {instance_id}
task.json: {RUNS / instance_id / "task.json"}
base_commit: {task.get("base_commit") or ""}
A diff: {folder / "a.diff"}
B diff: {folder / "b.diff"}
manifest: {folder / "manifest.json"}
A session log: {logs / "a.log"}
B session log: {logs / "b.log"}
A test log: {logs / "a-test.log"}
B test log: {logs / "b-test.log"}

diff 文件就是这一侧相对 base_commit 的 git diff。manifest 里 empty 为 true，或对应 diff 文件不存在，表示这一侧没有提交，不要编造改动。不要克隆仓库，不要打开 rollout jsonl，不要读 a-record.log 或 b-record.log。本地工作区可以不存在。测试日志先看末尾，没有汇总再搜 FAILED、ERROR、AssertionError、TIMEOUT。

评完只返回一个 JSON 对象，不要 Markdown。键必须是 A-测试结论、A-交付完整性（1-5）、A-交付完整性描述、B-测试结论、B-交付完整性（1-5）、B-交付完整性描述、GSB 结论、GSB 理由。分数是 1 到 5 的整数。某一侧没法打分时，那一侧的三个字段用 null。GSB 结论只能是 A 更好、Same、B 更好。
"""


def strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return stripped


def parse_json_fields(text: str) -> dict | None:
    start = text.find("{")
    if start < 0:
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def parse_plain_fields(text: str) -> dict | None:
    positions: list[tuple[int, str, int]] = []
    for label in FIELDS:
        for mark in (f"{label}：", f"{label}:"):
            found = text.find(mark)
            if found >= 0:
                positions.append((found, label, found + len(mark)))
                break
    if len(positions) != len(FIELDS):
        return None
    positions.sort()
    payload: dict[str, str] = {}
    for index, (_, label, value_at) in enumerate(positions):
        end = positions[index + 1][0] if index + 1 < len(positions) else len(text)
        payload[label] = text[value_at:end].strip()
    return payload


def normalize(payload: dict) -> dict:
    record: dict = {}
    missing = [label for label in FIELDS if label not in payload]
    if missing:
        raise ValueError("missing fields: " + ", ".join(missing))
    for label in FIELDS:
        value = payload[label]
        if value is None or (isinstance(value, str) and value.strip().lower() in {"", "null", "none"}):
            record[label] = None
            continue
        if label in SCORE_FIELDS:
            try:
                score = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{label} is not an integer") from exc
            if score < 1 or score > 5:
                raise ValueError(f"{label} is outside 1-5")
            record[label] = score
            continue
        if label == "GSB 结论":
            verdict = str(value).strip()
            if verdict not in VERDICTS:
                raise ValueError(f"bad GSB verdict: {verdict}")
            record[label] = verdict
            continue
        record[label] = str(value).strip()
    return record


def parse_result(text: str) -> dict:
    cleaned = strip_fence(text or "")
    payload = parse_json_fields(cleaned) or parse_plain_fields(cleaned)
    if payload is None:
        raise ValueError("agent result is not the eight fields")
    return normalize(payload)


def install_windows_bridge_fix() -> None:
    """Windows select() only accepts sockets. The SDK waits on a stderr pipe."""
    if os.name != "nt":
        return
    import cursor_sdk._bridge as bridge

    def _read_discovery(process, timeout):
        if process.stderr is None:
            raise bridge.CursorSDKError("Bridge process stderr is unavailable")
        stderr_fd = process.stderr.fileno()
        was_blocking = os.get_blocking(stderr_fd)
        os.set_blocking(stderr_fd, False)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        deadline = time.monotonic() + timeout
        stderr_lines: list[str] = []
        pending = ""

        def drain_available():
            nonlocal pending
            while True:
                try:
                    chunk = os.read(stderr_fd, 8192)
                except BlockingIOError:
                    return None
                if not chunk:
                    final_text = decoder.decode(b"", final=True)
                    if final_text:
                        pending += final_text
                    if pending:
                        line = pending
                        pending = ""
                        stderr_lines.append(line)
                        return bridge.parse_discovery_line(line)
                    return None
                pending += decoder.decode(chunk)
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    line += "\n"
                    stderr_lines.append(line)
                    discovery = bridge.parse_discovery_line(line)
                    if discovery is not None:
                        return discovery
            return None

        try:
            while time.monotonic() < deadline:
                discovery = drain_available()
                if discovery is not None:
                    return discovery
                exit_code = process.poll()
                if exit_code is not None:
                    discovery = drain_available()
                    if discovery is not None:
                        return discovery
                    raise bridge.CursorSDKError(
                        f"Bridge exited before discovery with status {exit_code}: "
                        + "".join(stderr_lines)
                        + pending
                    )
                time.sleep(0.05)
            raise bridge.CursorSDKError("Timed out waiting for bridge discovery")
        finally:
            os.set_blocking(stderr_fd, was_blocking)

    bridge._read_discovery = _read_discovery


def print_agent_reply(run) -> None:
    """Stream the agent's reply to the terminal. The caller still parses the final text."""
    shown: set[str] = set()
    for message in run.messages():
        kind = getattr(message, "type", "")
        if kind == "assistant":
            content = getattr(message, "message", None)
            for block in getattr(content, "content", None) or []:
                if getattr(block, "type", None) == "text":
                    text = getattr(block, "text", "") or ""
                    if text:
                        print(text, end="", flush=True)
            continue
        if kind != "tool_call":
            continue
        name = getattr(message, "name", "") or "tool"
        status = getattr(message, "status", "") or ""
        if status not in ("", "running", "started"):
            continue
        key = str(getattr(message, "call_id", None) or getattr(message, "id", None) or f"{name}:{len(shown)}")
        if key in shown:
            continue
        shown.add(key)
        print(f"\n  [tool] {name}", flush=True)
    print(flush=True)


def score_instance(instance_id: str, model: str, api_key: str) -> dict:
    install_windows_bridge_fix()
    from cursor_sdk import Agent, AgentOptions, CursorAgentError, LocalAgentOptions

    prompt = build_prompt(instance_id, DIFF_ROOT / instance_id)
    options = AgentOptions(
        model=model,
        api_key=api_key,
        tools=list(READ_TOOLS),
        local=LocalAgentOptions(cwd=str(RUNS.parent)),
    )
    last_error: CursorAgentError | None = None
    for attempt in range(3):
        try:
            with Agent.create(options) as agent:
                print(f"  agent {agent.agent_id}", flush=True)
                run = agent.send(prompt)
                print(f"  run {run.id}", flush=True)
                print_agent_reply(run)
                result = run.wait()
            break
        except CursorAgentError as exc:
            last_error = exc
            if not exc.is_retryable or attempt == 2:
                request = f" request_id={exc.request_id}" if exc.request_id else ""
                raise RuntimeError(f"{exc.message}{request}") from exc
            delay = 2 ** attempt
            if exc.retry_after:
                try:
                    delay = max(delay, int(float(exc.retry_after)))
                except ValueError:
                    pass
            print(f"  retry in {delay}s: {exc.message}", flush=True)
            time.sleep(delay)
    else:
        raise RuntimeError(str(last_error) if last_error else "agent did not start")
    if result.status != "finished":
        raise RuntimeError(f"run {result.status}: {result.id}")
    fields = parse_result(result.result or "")
    return {"instance_id": instance_id, "evaluated_at": utc_now(), **fields}


def main() -> int:
    parser = argparse.ArgumentParser(description="Score successful GSB tasks with a Cursor agent")
    parser.add_argument("--instance", action="append", default=[], help="only these instance ids; repeatable")
    parser.add_argument("--limit", type=int, default=None, help="score at most N tasks")
    parser.add_argument("--force", action="store_true", help="score again even when evals.json already has the task")
    parser.add_argument("--list", action="store_true", help="print the tasks that would be scored")
    parser.add_argument("--model", default=os.getenv("GSB_EVAL_MODEL") or "composer-2.5")
    args = parser.parse_args()
    only = set(args.instance)
    ids = pending_ids(only, args.force)
    if args.limit is not None:
        ids = ids[: args.limit]
    if args.list:
        for instance_id in ids:
            print(instance_id)
        print(f"{len(ids)} pending", file=sys.stderr)
        return 0
    api_key = os.getenv("CURSOR_API_KEY") or ""
    if not api_key:
        print("CURSOR_API_KEY is not set", file=sys.stderr)
        return 1
    if not ids:
        print("no pending tasks")
        return 0
    failed = 0
    for index, instance_id in enumerate(ids, start=1):
        folder = DIFF_ROOT / instance_id
        print(f"[{index}/{len(ids)}] {instance_id}", flush=True)
        try:
            prepare_diffs(instance_id)
            record = score_instance(instance_id, args.model, api_key)
            save_record(record)
            print(f"  saved {record['GSB 结论']}", flush=True)
        except Exception as exc:  # noqa: BLE001 - one task must not stop the rest
            failed += 1
            print(f"  failed: {exc}", file=sys.stderr, flush=True)
        finally:
            remove_diffs(folder)
    print(f"scored {len(ids) - failed}  failed {failed}  {EVALS}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
