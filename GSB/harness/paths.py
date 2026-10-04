"""Paths and fixed settings for the GSB harness."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


def env_path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value or default).expanduser().resolve()


ROOT = env_path("GSB_ROOT", Path(__file__).resolve().parents[1])
CANDIDATES = ROOT / "candidates-3000.jsonl"
SOURCE = env_path("GSB_SOURCE", ROOT.parent / "Multi-SWE-bench" / "SWE-rebench" / "SWE-rebench")
CODEX_LINUX = env_path("GSB_CODEX_LINUX", ROOT / "codex-linux")
CODEX_HOME = env_path("CODEX_HOME", Path.home() / ".codex")
RUNS = env_path("GSB_RUNS", ROOT / "runs")
WORK = env_path("GSB_WORK", ROOT / "task")
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

MODEL_PROVIDER = os.getenv("GSB_MODEL_PROVIDER") or "super_relay"
MODEL = os.getenv("GSB_MODEL") or "auto_model/urm"
EXPECTED_WINDOW = int(os.getenv("GSB_MODEL_CONTEXT_WINDOW") or "1000000")
PUSH_URL = os.getenv("GSB_PUSH_URL") or "https://github.com/waibiwaib1/GSB10012026.git"


def snapshot_url(sha: str) -> str:
    if not sha:
        return ""
    repo = PUSH_URL.removesuffix(".git")
    return f"{repo}/commit/{sha}"
S3_BUCKET = os.getenv("GSB_S3_BUCKET") or "gsb10012026-591821376517-ap-northeast-2-an"
S3_REGION = os.getenv("GSB_S3_REGION") or "ap-northeast-2"
