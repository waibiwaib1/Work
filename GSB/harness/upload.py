"""Upload a side's rollout and test recording to S3."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import boto3

from paths import ROOT, RUNS, S3_BUCKET, S3_REGION
from records import build_sheet_row, load_task, store_side, update_status, upsert_sheet_row
from runtime import side_branch

CONFIG_PATH = ROOT / "config.json"


def load_s3_config() -> dict[str, str]:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"missing {CONFIG_PATH}")
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    bucket = str(data.get("bucket") or S3_BUCKET)
    region = str(data.get("region") or S3_REGION)
    key_id = str(data.get("aws_access_key_id") or "")
    secret = str(data.get("aws_secret_access_key") or "")
    if not key_id or not secret:
        raise RuntimeError(f"{CONFIG_PATH} needs aws_access_key_id and aws_secret_access_key")
    return {"bucket": bucket, "region": region, "aws_access_key_id": key_id, "aws_secret_access_key": secret}


def object_url(key: str, bucket: str, region: str) -> str:
    return f"https://{bucket}.s3.{region}.amazonaws.com/{quote(key, safe='/')}"


def artifact_key(instance_id: str, side: str, suffix: str) -> str:
    return side_branch(instance_id, side).replace("/", "_") + suffix


def object_headers(key: str) -> dict[str, str]:
    filename = key.rsplit("/", 1)[-1]
    if key.endswith(".mp4"):
        content_type = "video/mp4"
    else:
        content_type = "text/plain; charset=utf-8"
    return {"ContentType": content_type, "ContentDisposition": f'inline; filename="{filename}"'}


def s3_client(config: dict[str, str]):
    return boto3.client(
        "s3",
        region_name=config["region"],
        aws_access_key_id=config["aws_access_key_id"],
        aws_secret_access_key=config["aws_secret_access_key"],
    )


def url_key(url: str) -> str:
    return unquote(urlparse(url).path.lstrip("/"))


def upload_file(path: Path, key: str) -> str:
    config = load_s3_config()
    print(f"upload s3://{config['bucket']}/{key}", flush=True)
    s3_client(config).upload_file(
        str(path),
        config["bucket"],
        key,
        ExtraArgs=object_headers(key),
    )
    return object_url(key, config["bucket"], config["region"])


def place_file(path: Path, key: str, current_url: str) -> str:
    """Keep an object under the branch-style key. An existing object is renamed in S3."""
    config = load_s3_config()
    if current_url and url_key(current_url) == key:
        return current_url
    client = s3_client(config)
    headers = object_headers(key)
    old_key = url_key(current_url) if current_url else ""
    if old_key and old_key != key:
        try:
            client.head_object(Bucket=config["bucket"], Key=old_key)
        except client.exceptions.ClientError:
            old_key = ""
        else:
            print(f"rename s3://{config['bucket']}/{old_key} -> {key}", flush=True)
            client.copy_object(
                Bucket=config["bucket"],
                Key=key,
                CopySource={"Bucket": config["bucket"], "Key": old_key},
                MetadataDirective="REPLACE",
                ContentType=headers["ContentType"],
                ContentDisposition=headers["ContentDisposition"],
            )
            client.delete_object(Bucket=config["bucket"], Key=old_key)
            return object_url(key, config["bucket"], config["region"])
    return upload_file(path, key)


def upload_side(instance_id: str, side: str, info: dict) -> bool:
    """Put local artifacts on S3 under the branch-style name. Returns True when every present file is stored."""
    ready = True
    rollout = info.get("rollout_file") or str(RUNS / instance_id / "artifacts" / f"{side}-rollout.jsonl")
    rollout_path = Path(rollout)
    if rollout_path.exists():
        info["s3_rollout"] = place_file(
            rollout_path, artifact_key(instance_id, side, ".jsonl"), info.get("s3_rollout") or ""
        )
        info["rollout_file"] = info.get("rollout_file") or str(rollout_path)
        ready = ready and bool(info.get("s3_rollout"))
    video = info.get("test_video") or str(RUNS / instance_id / "artifacts" / f"{side}-test.mp4")
    video_path = Path(video)
    if video_path.exists():
        info["s3_url"] = place_file(video_path, artifact_key(instance_id, side, ".mp4"), info.get("s3_url") or "")
        info["test_video"] = info.get("test_video") or str(video_path)
        ready = ready and bool(info.get("s3_url"))
    return ready and bool(info.get("s3_rollout") or info.get("s3_url"))


def upload_task(instance_id: str) -> int:
    record = load_task(instance_id)
    if not record:
        print(f"no task record: {instance_id}", file=sys.stderr)
        return 1
    failed = 0
    for side in ("a", "b"):
        info = (record.get("sides") or {}).get(side) or {}
        try:
            done = upload_side(instance_id, side, info)
        except Exception as exc:  # noqa: BLE001 - report the side and keep going
            failed += 1
            print(f"{instance_id} {side} upload failed: {exc}", flush=True)
            continue
        if not done:
            continue
        stored = (load_task(instance_id).get("sides") or {}).get(side) or {}
        stored.update(info)
        store_side(instance_id, side, stored)
        update_status(instance_id, **{f"s3_{side}": "done"})
    try:
        upsert_sheet_row(build_sheet_row({"instance_id": instance_id}))
    except PermissionError:
        print("sheet is open, left the S3 urls in task.json only")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload rollouts and test recordings to S3")
    parser.add_argument("--instance", action="append", required=True)
    args = parser.parse_args()
    failed = 0
    for instance_id in args.instance:
        failed += upload_task(instance_id)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
