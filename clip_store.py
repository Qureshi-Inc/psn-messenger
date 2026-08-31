"""Permanent clip archive storage abstraction.

Uses S3/MinIO when CLIP_BUCKET is set (boto3 already in requirements).
Falls back to local filesystem at /data/clips/ otherwise.
"""

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

CLIP_BUCKET         = os.environ.get("CLIP_BUCKET", "")
CLIP_S3_ENDPOINT    = os.environ.get("CLIP_S3_ENDPOINT_URL", "")
CLIP_LOCAL_DIR      = Path(os.environ.get("CLIP_LOCAL_DIR", "/data/clips"))

# Safety: log storage key but NOT signed URLs
_MAX_CLIP_BYTES     = int(os.environ.get("CLIP_MAX_BYTES", str(500 * 1024 * 1024)))  # 500 MB


def storage_key(message_uid: str, psn_created_at: float | None = None) -> str:
    """Deterministic, filesystem-safe path for a clip.

    message_uid may contain '#' and other special chars, so we replace
    any non-alphanumeric/underscore/hyphen character with underscore.
    """
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", message_uid)
    if psn_created_at:
        dt = datetime.fromtimestamp(psn_created_at, tz=timezone.utc)
        return f"clips/original/{dt.year}/{dt.month:02d}/{safe}.mp4"
    return f"clips/original/undated/{safe}.mp4"


def _s3_client():
    import boto3
    kwargs: dict = {}
    if CLIP_S3_ENDPOINT:
        kwargs["endpoint_url"] = CLIP_S3_ENDPOINT
    return boto3.client("s3", **kwargs)


def archive(key: str, data: bytes) -> bool:
    """Store MP4 bytes at key. Returns True on success."""
    if len(data) > _MAX_CLIP_BYTES:
        logger.error("clip_store: refusing to archive %d bytes (limit %d) key=%s",
                     len(data), _MAX_CLIP_BYTES, key)
        return False

    if CLIP_BUCKET:
        try:
            _s3_client().put_object(
                Bucket=CLIP_BUCKET,
                Key=key,
                Body=data,
                ContentType="video/mp4",
            )
            logger.info("clip_store: archived s3://%s/%s (%d bytes)",
                        CLIP_BUCKET, key, len(data))
            return True
        except Exception as exc:
            logger.error("clip_store: S3 archive failed key=%s: %s", key, exc)
            return False

    path = CLIP_LOCAL_DIR / key
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        logger.info("clip_store: archived local %s (%d bytes)", path, len(data))
        return True
    except Exception as exc:
        logger.error("clip_store: local archive failed key=%s: %s", key, exc)
        return False


def load(key: str) -> bytes | None:
    """Load MP4 bytes by key. Returns None on failure."""
    if CLIP_BUCKET:
        try:
            resp = _s3_client().get_object(Bucket=CLIP_BUCKET, Key=key)
            data = resp["Body"].read()
            logger.info("clip_store: loaded s3://%s/%s (%d bytes)",
                        CLIP_BUCKET, key, len(data))
            return data
        except Exception as exc:
            logger.error("clip_store: S3 load failed key=%s: %s", key, exc)
            return None

    path = CLIP_LOCAL_DIR / key
    if not path.exists():
        logger.error("clip_store: local file not found: %s", path)
        return None
    data = path.read_bytes()
    logger.info("clip_store: loaded local %s (%d bytes)", path, len(data))
    return data


def available() -> bool:
    """True if storage is configured and writable."""
    if CLIP_BUCKET:
        try:
            _s3_client().head_bucket(Bucket=CLIP_BUCKET)
            return True
        except Exception:
            return False
    try:
        CLIP_LOCAL_DIR.mkdir(parents=True, exist_ok=True)
        return True
    except Exception:
        return False


def backend() -> str:
    return f"s3://{CLIP_BUCKET}" if CLIP_BUCKET else str(CLIP_LOCAL_DIR)
