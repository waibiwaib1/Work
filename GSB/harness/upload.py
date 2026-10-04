"""Upload a side's rollout and test recording to S3."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from paths import RUNS, S3_BUCKET, S3_REGION
from records import load_task, store_side, update_status
from runtime import side_branch

def load_s3_config() -> dict[str, str]:
    return {
        "bucket": os.getenv("GSB_S3_BUCKET") or S3_BUCKET,
        "region": os.getenv("GSB_S3_REGION") or S3_REGION,
        "aws_access_key_id": os.getenv("AWS_ACCESS_KEY_ID") or "",
        "aws_secret_access_key": os.getenv("AWS_SECRET_ACCESS_KEY") or "",
        "aws_session_token": os.getenv("AWS_SESSION_TOKEN") or "",
    }


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
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("boto3 is required for S3 upload; install requirements.txt") from exc
    options = {"region_name": config["region"]}
    if config["aws_access_key_id"] and config["aws_secret_access_key"]:
        options["aws_access_key_id"] = config["aws_access_key_id"]
        options["aws_secret_access_key"] = config["aws_secret_access_key"]
        if config["aws_session_token"]:
            options["aws_session_token"] = config["aws_session_token"]
    return boto3.client("s3", **options)


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


def same_object(config: dict[str, str], key: str, path: Path) -> bool:
    try:
        head = s3_client(config).head_object(Bucket=config["bucket"], Key=key)
    except Exception:
        return False
    return int(head.get("ContentLength") or -1) == path.stat().st_size


def place_file(path: Path, key: str, current_url: str) -> str:
    """Keep an object under the branch-style key. An existing object is renamed in S3."""
    config = load_s3_config()
    if current_url and url_key(current_url) == key and same_object(config, key, path):
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
    if video_path.is_file() and video_path.stat().st_size > 0:
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
