"""Paths and fixed settings for the GSB harness."""

from pathlib import Path

ROOT = Path(r"D:\Code\Work\GSB")
CANDIDATES = ROOT / "candidates-3000.jsonl"
SOURCE = Path(r"D:\Code\Work\Multi-SWE-bench\SWE-rebench\SWE-rebench")
CODEX_LINUX = ROOT / "codex-linux"
CODEX_HOME = Path.home() / ".codex"
RUNS = Path(r"D:\Code\Work\GSB\runs")
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
PUSH_URL = "https://github.com/waibiwaib1/GSB10012026.git"


def snapshot_url(sha: str) -> str:
    if not sha:
        return ""
    repo = PUSH_URL.removesuffix(".git")
    return f"{repo}/commit/{sha}"
S3_BUCKET = "gsb10012026-591821376517-ap-northeast-2-an"
S3_REGION = "ap-northeast-2"
