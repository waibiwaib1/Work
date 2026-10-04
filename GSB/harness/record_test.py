"""Record one successful side while its test command runs.

The test has no window of its own. This opens a console, shows the test
output there, and records that window. Sides run one at a time.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

from paths import RUNS, WORK
from records import (
    load_state,
    load_task,
    store_side,
    task_outcome,
    update_status,
)
from runtime import docker_pull, remove_test_container, test_container_name

CREATE_NEW_CONSOLE = 0x00000010
RECORD_TAIL_SECONDS = 90
SW_MAXIMIZE = 3
SW_RESTORE = 9
_record_lock = threading.Lock()
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040


def find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    candidates = [
        Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
        Path.home() / "scoop" / "shims" / "ffmpeg.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe",
    ]
    packages = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if packages.is_dir():
        candidates.extend(packages.glob("Gyan.FFmpeg*/ffmpeg-*/bin/ffmpeg.exe"))
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


def console_title(instance_id: str, side: str) -> str:
    return f"gsb test {instance_id} {side}"


def find_window(title: str) -> int:
    user32 = ctypes.windll.user32
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    return int(user32.FindWindowW(None, title))


def place_window(hwnd: int, topmost: bool) -> None:
    user32 = ctypes.windll.user32
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = ctypes.c_bool
    user32.ShowWindow(hwnd, SW_MAXIMIZE if topmost else SW_RESTORE)
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    insert = wintypes.HWND(-1 if topmost else -2)
    user32.SetWindowPos(hwnd, insert, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)


def successful_tasks(instance_ids: list[str]) -> list[str]:
    tasks = (load_state().get("tasks") or {})
    chosen = instance_ids or list(tasks)
    ready = []
    for instance_id in chosen:
        task = tasks.get(instance_id)
        if task is None:
            print(f"not in status: {instance_id}")
            continue
        if task_outcome(task) != "success":
            print(f"skip {instance_id}: {task_outcome(task) or 'not finished'}")
            continue
        ready.append(instance_id)
    return ready


def already_recorded(instance_id: str, side: str) -> bool:
    task = (load_state().get("tasks") or {}).get(instance_id) or {}
    if task.get(f"video_{side}") != "done":
        return False
    path = ((load_task(instance_id).get("sides") or {}).get(side) or {}).get("test_video") or ""
    return bool(path and Path(path).exists())


def remember(instance_id: str, side: str, video: Path, test_exit: int | None) -> None:
    info = (load_task(instance_id).get("sides") or {}).get(side) or {}
    info["test_exit"] = test_exit
    info["test_video"] = str(video)
    info.pop("s3_url", None)
    store_side(instance_id, side, info)
    update_status(
        instance_id,
        **{f"video_{side}": "done", f"s3_{side}": "", "uploaded": False, "uploaded_at": None},
    )


def play(instance_id: str, side: str, timeout: int) -> int:
    task = load_task(instance_id)
    title = console_title(instance_id, side)
    ctypes.windll.kernel32.SetConsoleTitleW(title)
    console = ctypes.windll.kernel32.GetConsoleWindow()
    ctypes.windll.user32.ShowWindow(console, SW_MAXIMIZE)
    ready = RUNS / instance_id / "logs" / f".{side}-record.ready"
    go = RUNS / instance_id / "logs" / f".{side}-record.go"
    stop = RUNS / instance_id / "logs" / f".{side}-record.stop"
    ready.write_text("ready", encoding="utf-8")
    deadline = time.time() + 30
    while not go.exists():
        if stop.exists() or time.time() > deadline:
            print("recorder did not start")
            return 1
        time.sleep(0.2)
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
        task["image_name"],
        "-c",
        task.get("test_cmd") or "",
    ]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    remove_test_container(instance_id, side)
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert proc.stdout is not None

        def relay() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
                log.write(line.decode("utf-8", errors="replace"))
                log.flush()

        started = time.time()
        relay_thread = threading.Thread(target=relay)
        relay_thread.start()
        while relay_thread.is_alive() and time.time() - started <= timeout:
            relay_thread.join(0.5)
        timed_out = relay_thread.is_alive()
        if timed_out:
            remove_test_container(instance_id, side)
            proc.kill()
            proc.wait()
            relay_thread.join()
            message = f"\nTIMEOUT after {timeout}s\n"
            sys.stdout.write(message)
            log.write(message)
            print(message.strip())
            time.sleep(2)
            return 124
        code = proc.wait()
    print(f"exit {code}")
    time.sleep(2)
    return code


def keep_last_seconds(ffmpeg: str, video: Path, seconds: int = RECORD_TAIL_SECONDS) -> None:
    ffprobe = str(Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe"))
    probed = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    try:
        duration = float(probed.stdout.strip())
    except ValueError:
        return
    if duration <= seconds + 1:
        return
    trimmed = video.with_name(f"{video.stem}-tail{video.suffix}")
    completed = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-sseof",
            f"-{seconds}",
            "-i",
            str(video),
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(trimmed),
        ],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    if completed.returncode != 0 or not trimmed.exists() or trimmed.stat().st_size == 0:
        trimmed.unlink(missing_ok=True)
        return
    trimmed.replace(video)


def stop_ffmpeg(proc: subprocess.Popen[bytes]) -> None:
    try:
        if proc.stdin:
            proc.stdin.write(b"q\n")
            proc.stdin.flush()
    except OSError:
        proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait(timeout=10)


def record_side(instance_id: str, side: str, timeout: int, ffmpeg: str, force: bool) -> int:
    if not force and already_recorded(instance_id, side):
        print(f"skip {instance_id} {side}: video already saved")
        return 0
    task = load_task(instance_id)
    test_cmd = task.get("test_cmd") or ""
    if not test_cmd:
        print(f"skip {instance_id} {side}: no test_cmd")
        return 0
    work_dir = WORK / instance_id / side
    if not work_dir.exists():
        print(f"skip {instance_id} {side}: missing {work_dir}")
        return 1
    with _record_lock:
        return _record_side_locked(instance_id, side, timeout, ffmpeg, task)


def _record_side_locked(instance_id: str, side: str, timeout: int, ffmpeg: str, task: dict) -> int:
    logs = RUNS / instance_id / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    for name in (f".{side}-record.ready", f".{side}-record.go", f".{side}-record.stop"):
        target = logs / name
        if target.exists():
            target.unlink()
    video = RUNS / instance_id / "artifacts" / f"{side}-test.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    print(f"pull {task['image_name']}", flush=True)
    docker_pull(task["image_name"])
    title = console_title(instance_id, side)
    play_proc = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--play",
            "--instance",
            instance_id,
            "--side",
            side,
            "--test-timeout",
            str(timeout),
        ],
        creationflags=CREATE_NEW_CONSOLE,
    )
    ready = logs / f".{side}-record.ready"
    hwnd = 0
    deadline = time.time() + 20
    while time.time() < deadline and play_proc.poll() is None:
        if ready.exists():
            hwnd = find_window(title)
            if hwnd:
                break
        time.sleep(0.2)
    if not hwnd:
        (logs / f".{side}-record.stop").write_text("stop", encoding="utf-8")
        play_proc.wait(timeout=10)
        print(f"could not find the test window for {instance_id} {side}")
        return 1
    place_window(hwnd, True)
    ff_log = logs / f"{side}-record.log"
    with ff_log.open("w", encoding="utf-8", errors="replace") as handle:
        ff = subprocess.Popen(
            [
                ffmpeg,
                "-y",
                "-f",
                "gdigrab",
                "-framerate",
                "8",
                "-draw_mouse",
                "1",
                "-i",
                f"hwnd=0x{hwnd:x}",
                "-vf",
                "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-an",
                str(video),
            ],
            stdin=subprocess.PIPE,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        time.sleep(0.8)
        if ff.poll() is not None:
            (logs / f".{side}-record.stop").write_text("stop", encoding="utf-8")
            play_proc.wait(timeout=10)
            print(f"ffmpeg failed, see {ff_log}")
            return 1
        (logs / f".{side}-record.go").write_text("go", encoding="utf-8")
        print(f"recording {video}", flush=True)
        test_exit = play_proc.wait()
        stop_ffmpeg(ff)
    place_window(hwnd, False)
    if not video.exists() or video.stat().st_size == 0:
        print(f"no video written for {instance_id} {side}")
        return 1
    keep_last_seconds(ffmpeg, video)
    remembered = None if test_exit == 124 else test_exit
    remember(instance_id, side, video, remembered)
    print(f"saved {video} test_exit={remembered}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run tests for successful tasks and record the window")
    parser.add_argument("--instance", action="append", default=[], help="only these successful tasks; repeatable")
    parser.add_argument("--side", choices=("a", "b"), help="record one side; default is A then B")
    parser.add_argument("--test-timeout", type=int, default=1800)
    parser.add_argument("--force", action="store_true", help="record again even when a video is already saved")
    parser.add_argument("--play", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.play:
        if len(args.instance) != 1 or not args.side:
            print("--play needs one --instance and --side", file=sys.stderr)
            return 2
        return play(args.instance[0], args.side, args.test_timeout)
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("ffmpeg is not installed", file=sys.stderr)
        return 1
    ready = successful_tasks(args.instance)
    if not ready:
        print("no successful tasks")
        return 1 if args.instance else 0
    sides = (args.side,) if args.side else ("a", "b")
    failed = 0
    for instance_id in ready:
        for side in sides:
            try:
                failed += record_side(instance_id, side, args.test_timeout, ffmpeg, args.force)
            except Exception as exc:  # noqa: BLE001 - one side should not stop the rest
                failed += 1
                print(f"{instance_id} {side} failed: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
