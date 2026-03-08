"""
media_manager.py
----------------
Handles all media file operations for post campaigns:
  - Pre-flight verification that files still exist in Supabase Storage
  - Downloading files from Storage to a local temp directory
  - Cleaning up local temp files after a campaign finishes
  - Cleaning up files from Supabase Storage after a campaign finishes
  - On-startup orphan cleanup (handles interrupted campaigns)

All files are stored under:
  Supabase Storage:  campaign-media/{campaign_id}/{file_name}
  Local filesystem:  bot-media/{campaign_id}/{file_name}
"""

import os
import shutil
import time
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Injected by app.py after Supabase client is initialised
_supabase = None
STORAGE_BUCKET = "campaign-media"
LOCAL_MEDIA_DIR = "bot-media"
ORPHAN_MAX_AGE_SECONDS = 86400  # 24 hours


def init_media_manager(supabase_client):
    """Call once at startup to inject the Supabase client."""
    global _supabase
    _supabase = supabase_client


# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight verification
# ─────────────────────────────────────────────────────────────────────────────

def verify_media_exists_in_storage(media_attachments: list[dict]) -> tuple[bool, list[str]]:
    """
    Verify every attachment still exists in Supabase Storage by attempting
    to generate a signed URL for each one.

    Returns:
      (all_exist: bool, missing_paths: list[str])
    """
    missing = []
    for attachment in media_attachments:
        storage_path = attachment if isinstance(attachment, str) else attachment["storage_path"]
        try:
            result = _supabase.storage.from_(STORAGE_BUCKET).create_signed_url(
                storage_path, expires_in=60
            )
            # If the file doesn't exist, Supabase returns an error key
            if result.get("error") or not result.get("signedURL"):
                missing.append(storage_path)
        except Exception as e:
            logger.warning(f"Could not verify {storage_path}: {e}")
            missing.append(storage_path)

    return len(missing) == 0, missing


# ─────────────────────────────────────────────────────────────────────────────
# Download
# ─────────────────────────────────────────────────────────────────────────────

def download_campaign_media(campaign_id: str, media_attachments: list[dict]) -> list[str]:
    """
    Download all media files for a campaign from Supabase Storage to the
    local bot-media/{campaign_id}/ directory.

    Returns an ordered list of absolute local file paths matching the order
    of media_attachments (which reflects the order the user uploaded them).
    """
    local_dir = Path(LOCAL_MEDIA_DIR) / campaign_id
    local_dir.mkdir(parents=True, exist_ok=True)

    local_paths = []
    for attachment in media_attachments:
        if isinstance(attachment, str):
            storage_path = attachment
            file_name = attachment.rsplit("/", 1)[-1]
        else:
            storage_path = attachment["storage_path"]
            file_name = attachment["file_name"]
        local_path = local_dir / file_name

        logger.info(f"Downloading {storage_path} → {local_path}")
        file_bytes = _supabase.storage.from_(STORAGE_BUCKET).download(storage_path)

        with open(local_path, "wb") as f:
            f.write(file_bytes)

        local_paths.append(str(local_path.resolve()))

    logger.info(f"Downloaded {len(local_paths)} file(s) for campaign {campaign_id}")
    return local_paths


# ─────────────────────────────────────────────────────────────────────────────
# Cleanup
# ─────────────────────────────────────────────────────────────────────────────

def delete_local_campaign_dir(campaign_id: str) -> None:
    """Delete the local temp directory for a campaign."""
    local_dir = Path(LOCAL_MEDIA_DIR) / campaign_id
    if local_dir.exists():
        shutil.rmtree(local_dir, ignore_errors=True)
        logger.info(f"Deleted local media dir: {local_dir}")


def delete_campaign_media_from_storage(media_attachments: list[dict]) -> None:
    """
    Delete all files belonging to a campaign from Supabase Storage.
    Called after a campaign completes, fails, or is aborted.
    """
    paths = [a if isinstance(a, str) else a["storage_path"] for a in media_attachments]
    if not paths:
        return
    try:
        _supabase.storage.from_(STORAGE_BUCKET).remove(paths)
        logger.info(f"Deleted {len(paths)} file(s) from Supabase Storage")
    except Exception as e:
        logger.warning(f"Storage cleanup error (non-fatal): {e}")


def cleanup_orphan_temp_files() -> None:
    """
    On server startup, delete any bot-media/ subdirectories older than 24 hours.
    These are leftovers from campaigns that were interrupted mid-run and never cleaned up.
    """
    media_root = Path(LOCAL_MEDIA_DIR)
    if not media_root.exists():
        return

    now = time.time()
    for subdir in media_root.iterdir():
        if not subdir.is_dir():
            continue
        age = now - subdir.stat().st_mtime
        if age > ORPHAN_MAX_AGE_SECONDS:
            shutil.rmtree(subdir, ignore_errors=True)
            logger.info(f"Orphan cleanup: removed {subdir} (age: {age/3600:.1f}h)")
