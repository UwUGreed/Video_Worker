#!/usr/bin/env python3

import hashlib
import hmac
import json
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import fcntl

SHORTS_DIR = Path(__file__).resolve().parent
REPO_DIR = SHORTS_DIR.parent


def load_env_file(path):
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip("'").strip('"')


def configured_dir(env_name, default_path):
    raw = (os.environ.get(env_name) or "").strip()
    if not raw:
        return Path(default_path).resolve()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = REPO_DIR / candidate
    return candidate.resolve()


def resolve_optional_path(raw_value, default_path, *, relative_base=None):
    raw = (raw_value or "").strip()
    if not raw:
        return Path(default_path).resolve()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = (relative_base or REPO_DIR) / candidate
    return candidate.resolve()


for env_name in ("youtube.env", "instagram.env", "tiktok.env"):
    load_env_file(SHORTS_DIR / env_name)

CLIPS_DIR = configured_dir("VIDEO_SHORTS_CLIPS_DIR", SHORTS_DIR / "clips")
STATE_DIR = configured_dir("VIDEO_SHORTS_STATE_DIR", SHORTS_DIR / "state")
QUEUE_PATH = STATE_DIR / "queue.json"
LOCK_PATH = STATE_DIR / "queue.lock"
QUICK_TUNNEL_LOCK_PATH = STATE_DIR / "quick_tunnel.lock"
QUICK_TUNNEL_STATE_PATH = STATE_DIR / "quick_tunnel_state.json"
BUFFER_CHANNEL_CACHE_PATH = STATE_DIR / "buffer_channel_cache.json"
BUFFER_SCHEMA_CACHE_PATH = STATE_DIR / "buffer_schema_cache.json"
OUT_DIR = REPO_DIR / "out"
OUT_SNAPSHOT_NAMES = {"current", "current_shorts"}
DEFAULT_TOKEN_PATH = SHORTS_DIR / "credentials" / "token.json"
DEFAULT_CLIENT_SECRET_PATH = SHORTS_DIR / "credentials" / "client_secret.json"
POSTING_LEASE_HOURS = 6
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
DEFAULT_DELETE_POSTED_SHORTS = True
DEFAULT_POSTED_QUEUE_KEEP = 120
DEFAULT_POSTED_QUEUE_RETENTION_DAYS = 21
DEFAULT_RENDER_KEEP_JOBS = 6
DEFAULT_RENDER_RETENTION_DAYS = 2
DEFAULT_RENDER_MAX_GB = 8.0
DEFAULT_RENDER_MIN_AGE_MINUTES = 60
DEFAULT_ORPHAN_CLIP_RETENTION_DAYS = 2
ACTIVE_RENDER_MARKERS = {".shorts_in_progress"}
ACTIVE_RENDER_FILES = {"final.encoding.mp4"}
DEFAULT_INSTAGRAM_API_VERSION = "v25.0"
DEFAULT_INSTAGRAM_POLL_SECONDS = 5
DEFAULT_INSTAGRAM_POLL_TIMEOUT_SECONDS = 300
DEFAULT_INSTAGRAM_PUBLISH_METHOD = "quick_tunnel"
DEFAULT_INSTAGRAM_QUICK_TUNNEL_TIMEOUT_SECONDS = 45
DEFAULT_INSTAGRAM_QUICK_TUNNEL_GRACE_SECONDS = 90
DEFAULT_QUICK_TUNNEL_MIN_INTERVAL_SECONDS = 30
DEFAULT_QUICK_TUNNEL_MAX_ATTEMPTS = 2
DEFAULT_QUICK_TUNNEL_RETRY_DELAY_SECONDS = 45
DEFAULT_QUICK_TUNNEL_DOH_URL = "https://1.1.1.1/dns-query"
DEFAULT_QUICK_TUNNEL_SETTLE_SECONDS = 30
DEFAULT_INSTAGRAM_CTA_TEXT = "Dont forget to Like and follow"
DEFAULT_SHORTFORM_DESCRIPTION_TEXT = "3chan-style greentext story short."
DEFAULT_SHORTFORM_HASHTAGS = ["#shorts", "#greentext", "#storytime"]
DEFAULT_BUFFER_API_URL = "https://api.buffer.com"
DEFAULT_BUFFER_CLOUDFLARED_BIN = "cloudflared"
DEFAULT_BUFFER_QUICK_TUNNEL_TIMEOUT_SECONDS = DEFAULT_INSTAGRAM_QUICK_TUNNEL_TIMEOUT_SECONDS
DEFAULT_BUFFER_QUICK_TUNNEL_GRACE_SECONDS = DEFAULT_INSTAGRAM_QUICK_TUNNEL_GRACE_SECONDS
DEFAULT_BUFFER_QUICK_TUNNEL_SETTLE_SECONDS = 15
DEFAULT_BUFFER_TUNNEL_HOLD_SECONDS = 300
DEFAULT_INSTAGRAM_PUBLISH_BACKEND = "native"
DEFAULT_TIKTOK_PUBLISH_BACKEND = "native"
DEFAULT_TIKTOK_POLL_SECONDS = 5
DEFAULT_TIKTOK_POLL_TIMEOUT_SECONDS = 300
DEFAULT_TIKTOK_UPLOAD_CHUNK_SIZE_MB = 10
TIKTOK_TERMINAL_PUBLISH_STATUSES = {"PUBLISH_COMPLETE", "FAILED", "SEND_TO_USER_INBOX"}


def now_utc_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_iso8601(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def ensure_layout():
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (SHORTS_DIR / "credentials").mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)


def env_bool(name, default):
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def env_int(name, default, minimum=0):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(value, minimum)


def env_float(name, default, minimum=0.0):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(value, minimum)


def item_timestamp(item, *keys):
    for key in keys:
        parsed = parse_iso8601(item.get(key))
        if parsed is not None:
            return parsed
    return datetime.min.replace(tzinfo=timezone.utc)


def path_timestamp(path):
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return datetime.min.replace(tzinfo=timezone.utc)


def path_size_bytes(path):
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0

    total = 0
    for root, _, files in os.walk(path):
        for filename in files:
            candidate = Path(root) / filename
            try:
                total += candidate.stat().st_size
            except OSError:
                continue
    return total


def normalize_path(value):
    if not value:
        return ""
    return str(Path(value).resolve())


def unlink_if_exists(path):
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return True, None
    except OSError as exc:
        return False, str(exc)
    return True, None


def rmtree_if_exists(path):
    target = Path(path)
    if not target.exists():
        return True, None
    try:
        shutil.rmtree(target)
    except OSError as exc:
        return False, str(exc)
    return True, None


def atomic_write_json(path, payload):
    ensure_layout()
    path = Path(path)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=path.parent, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
        temp_name = handle.name
    os.replace(temp_name, path)


@contextmanager
def queue_lock():
    ensure_layout()
    with open(LOCK_PATH, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_queue_unlocked():
    if not QUEUE_PATH.exists():
        return []
    with open(QUEUE_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def save_queue_unlocked(queue):
    atomic_write_json(QUEUE_PATH, queue)


def sanitize_hashtags(values):
    tags = []
    for value in values:
        cleaned = value.strip()
        if not cleaned:
            continue
        if not cleaned.startswith("#"):
            cleaned = f"#{cleaned}"
        tags.append(cleaned)
    return tags


def sanitize_tags(values):
    return [value.strip() for value in values if value.strip()]


def queue_summary():
    with queue_lock():
        queue = load_queue_unlocked()
    return {
        "total": len(queue),
        "pending": sum(1 for item in queue if item.get("status") == "pending"),
        "posting": sum(1 for item in queue if item.get("status") == "posting"),
        "posted": sum(1 for item in queue if item.get("status") == "posted"),
    }


def register_generated_shorts(job_id, generated_items):
    ensure_layout()
    queued_at = now_utc_iso()
    entries = []

    with queue_lock():
        queue = load_queue_unlocked()

        for item in generated_items:
            entry = {
                "queue_id": uuid.uuid4().hex,
                "job_id": job_id,
                "clip_index": item["clip_index"],
                "file_path": item["output_path"],
                "title": item["youtube_title"],
                "description": item["youtube_description"],
                "tags": item["youtube_tags"],
                "hashtags": item["youtube_hashtags"],
                "headline": item.get("headline"),
                "source_start": item.get("source_start"),
                "source_end": item.get("source_end"),
                "status": "pending",
                "queued_at": queued_at,
                "last_attempted_at": None,
                "failed_attempts": 0,
                "last_error": None,
                "youtube_video_id": None,
                "youtube_url": None,
            }
            queue.append(entry)
            entries.append(entry)

        save_queue_unlocked(queue)

    return entries


def prune_posted_queue_entries(queue):
    keep_count = env_int("VIDEO_POSTED_QUEUE_KEEP", DEFAULT_POSTED_QUEUE_KEEP, minimum=0)
    retention_days = env_int(
        "VIDEO_POSTED_QUEUE_RETENTION_DAYS",
        DEFAULT_POSTED_QUEUE_RETENTION_DAYS,
        minimum=0,
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    posted_items = [item for item in queue if item.get("status") == "posted"]
    posted_items.sort(key=lambda item: item_timestamp(item, "posted_at", "queued_at"))

    kept_ids = set()
    if keep_count > 0:
        kept_ids = {item.get("queue_id") for item in posted_items[-keep_count:]}

    pruned_queue = []
    removed = 0
    for item in queue:
        if item.get("status") != "posted":
            pruned_queue.append(item)
            continue

        posted_at = item_timestamp(item, "posted_at", "queued_at")
        is_recent = posted_at >= cutoff
        within_keep_cap = keep_count > 0 and item.get("queue_id") in kept_ids
        if is_recent and within_keep_cap:
            pruned_queue.append(item)
            continue

        removed += 1

    return pruned_queue, removed


def release_stale_posting_items(queue):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=POSTING_LEASE_HOURS)
    changed = False

    for item in queue:
        if item.get("status") != "posting":
            continue
        attempted_at = parse_iso8601(item.get("last_attempted_at"))
        if attempted_at is None or attempted_at < cutoff:
            item["status"] = "pending"
            changed = True

    return changed


def reserve_next_short():
    with queue_lock():
        queue = load_queue_unlocked()
        changed = release_stale_posting_items(queue)

        selected = None
        for item in queue:
            if item.get("status") == "pending":
                item["status"] = "posting"
                item["last_attempted_at"] = now_utc_iso()
                item["last_error"] = None
                selected = dict(item)
                changed = True
                break

        if changed:
            save_queue_unlocked(queue)

    return selected


def update_queue_entry(queue_id, updater):
    with queue_lock():
        queue = load_queue_unlocked()
        changed = False
        for item in queue:
            if item.get("queue_id") != queue_id:
                continue
            updater(item)
            changed = True
            break
        if changed:
            save_queue_unlocked(queue)


def mark_posted(queue_id, youtube_video_id=None, youtube_url=None, file_cleanup=None, platform_results=None):
    file_cleanup = file_cleanup or {}
    platform_results = dict(platform_results or {})

    def updater(item):
        item["status"] = "posted"
        item["posted_at"] = now_utc_iso()
        if youtube_video_id is not None:
            item["youtube_video_id"] = youtube_video_id
        if youtube_url is not None:
            item["youtube_url"] = youtube_url
        for platform, result in platform_results.items():
            apply_queue_entry_platform_result(item, platform, result)
        item["last_error"] = None
        item["local_file_deleted"] = bool(file_cleanup.get("deleted"))
        item["local_file_deleted_at"] = now_utc_iso() if file_cleanup.get("deleted") else None
        item["local_file_delete_error"] = file_cleanup.get("error")

    update_queue_entry(queue_id, updater)


def mark_posted_partial(queue_id, *, platform_results=None, platform_errors=None, file_cleanup=None):
    file_cleanup = file_cleanup or {}
    platform_results = dict(platform_results or {})
    platform_errors = dict(platform_errors or {})

    def updater(item):
        item["status"] = "posted"
        item["posted_at"] = now_utc_iso()
        item["partial_success"] = True
        item["partial_platform_errors"] = platform_errors
        item["last_error"] = queue_platform_error_message(platform_errors) or None
        for platform, result in platform_results.items():
            apply_queue_entry_platform_result(item, platform, result)
        item["local_file_deleted"] = bool(file_cleanup.get("deleted"))
        item["local_file_deleted_at"] = now_utc_iso() if file_cleanup.get("deleted") else None
        item["local_file_delete_error"] = file_cleanup.get("error")

    update_queue_entry(queue_id, updater)


def mark_failed(queue_id, error_message):
    def updater(item):
        item["status"] = "pending"
        item["last_error"] = error_message
        item["failed_attempts"] = int(item.get("failed_attempts") or 0) + 1

    update_queue_entry(queue_id, updater)


def release_reserved(queue_id):
    def updater(item):
        item["status"] = "pending"

    update_queue_entry(queue_id, updater)


def delete_queue_entry(queue_id):
    with queue_lock():
        queue = load_queue_unlocked()
        original_count = len(queue)
        queue = [item for item in queue if item.get("queue_id") != queue_id]
        if len(queue) == original_count:
            return False
        save_queue_unlocked(queue)
    return True


def _split_env_list(name, default_values):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return list(default_values)
    return [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]


def build_entry_payload(entry):
    title = entry["title"]
    description = entry["description"]
    tags = sanitize_tags(_split_env_list("YOUTUBE_TAGS", entry.get("tags") or []))
    hashtags = sanitize_hashtags(_split_env_list("YOUTUBE_HASHTAGS", entry.get("hashtags") or []))

    if hashtags:
        hashtag_line = " ".join(hashtags)
        if hashtag_line not in description:
            description = f"{description.rstrip()}\n\n{hashtag_line}"

    title_suffix = (os.environ.get("YOUTUBE_TITLE_SUFFIX") or "").strip()
    if title_suffix and title_suffix not in title:
        title = f"{title}{title_suffix}"
    title = title[:100].rstrip()

    return {
        "title": title,
        "description": description,
        "tags": tags,
        "privacy_status": (os.environ.get("YOUTUBE_PRIVACY_STATUS") or "public").strip() or "public",
        "category_id": (os.environ.get("YOUTUBE_CATEGORY_ID") or "24").strip() or "24",
    }


def queue_platform_targets(force_all_platforms=False):
    enable_all = force_all_platforms or env_bool("VIDEO_POST_TO_ALL_PLATFORMS", False)
    targets = {
        "youtube": env_bool("VIDEO_POST_TO_YOUTUBE", True),
        "instagram": env_bool("VIDEO_POST_TO_INSTAGRAM", False),
        "tiktok": env_bool("VIDEO_POST_TO_TIKTOK", False),
    }
    if enable_all:
        targets["youtube"] = True
        targets["instagram"] = True
        targets["tiktok"] = True
    if not any(targets.values()):
        targets["youtube"] = True
    return targets


def build_queue_shortform_caption(entry):
    headline = (entry.get("headline") or "").strip()
    if headline:
        return headline

    title = (entry.get("title") or "").strip()
    title_suffix = (os.environ.get("YOUTUBE_TITLE_SUFFIX") or "").strip()
    if title_suffix and title.endswith(title_suffix):
        title = title[: -len(title_suffix)].rstrip()
    return title


def queue_entry_platform_results(entry):
    return dict(entry.get("platform_results") or {})


def queue_entry_platform_completed(entry, platform):
    platform_result = (queue_entry_platform_results(entry).get(platform) or {})
    if platform_result.get("completed"):
        return True
    if platform == "youtube":
        return bool(entry.get("youtube_video_id") or entry.get("youtube_url"))
    if platform == "instagram":
        return bool(entry.get("instagram_media_id") or entry.get("instagram_permalink"))
    if platform == "tiktok":
        return bool(entry.get("tiktok_publish_id") or entry.get("tiktok_public_post_ids"))
    return False


def apply_queue_entry_platform_result(item, platform, result):
    completed_at = now_utc_iso()
    platform_results = queue_entry_platform_results(item)
    normalized_result = dict(result or {})
    normalized_result["completed"] = True
    normalized_result["completed_at"] = completed_at
    platform_results[platform] = normalized_result
    item["platform_results"] = platform_results

    if platform == "youtube":
        item["youtube_video_id"] = normalized_result.get("video_id")
        item["youtube_url"] = normalized_result.get("youtube_url")
    elif platform == "instagram":
        item["instagram_media_id"] = normalized_result.get("media_id") or normalized_result.get("buffer_post_id")
        item["instagram_permalink"] = normalized_result.get("permalink")
        item["instagram_shortcode"] = normalized_result.get("shortcode")
    elif platform == "tiktok":
        item["tiktok_publish_id"] = normalized_result.get("publish_id") or normalized_result.get("buffer_post_id")
        item["tiktok_public_post_ids"] = normalized_result.get("public_post_ids") or []
        item["tiktok_provider"] = normalized_result.get("provider")


def persist_queue_entry_platform_result(queue_id, platform, result):
    def updater(item):
        apply_queue_entry_platform_result(item, platform, result)
        item["last_error"] = None

    update_queue_entry(queue_id, updater)


def persist_queue_entry_platform_progress(queue_id, platform, result):
    def updater(item):
        platform_results = queue_entry_platform_results(item)
        existing = dict(platform_results.get(platform) or {})
        merged = dict(existing)
        merged.update(dict(result or {}))
        platform_results[platform] = merged
        item["platform_results"] = platform_results
        item["last_error"] = None

    update_queue_entry(queue_id, updater)


def accept_buffer_platform_result(platform, result, *, followup_error="", followup_snapshot=None):
    normalized = dict(result or {})
    buffer_post_id = (normalized.get("buffer_post_id") or normalized.get("id") or "").strip()
    if not buffer_post_id:
        return {}

    normalized["buffer_post_id"] = buffer_post_id
    normalized["status"] = "accepted"
    normalized["platform"] = platform
    normalized["provider"] = "buffer"
    normalized["buffer_post_accepted"] = True
    if followup_error:
        normalized["buffer_followup_error"] = followup_error
    if followup_snapshot is not None:
        normalized["buffer_followup_snapshot"] = format_buffer_post_snapshot(followup_snapshot)
    return normalized


def accept_buffer_tiktok_result(result, *, followup_error="", followup_snapshot=None):
    return accept_buffer_platform_result(
        "tiktok",
        result,
        followup_error=followup_error,
        followup_snapshot=followup_snapshot,
    )


def accept_buffer_instagram_result(result, *, followup_error="", followup_snapshot=None):
    return accept_buffer_platform_result(
        "instagram",
        result,
        followup_error=followup_error,
        followup_snapshot=followup_snapshot,
    )


def queue_platform_error_message(errors):
    return "; ".join(f"{platform}: {message}" for platform, message in errors.items())


def should_finalize_queue_entry_after_social_failures(active_platforms, platform_results, platform_errors):
    if not env_bool("VIDEO_CONTINUE_ON_SOCIAL_FAILURE", True):
        return False
    if "youtube" not in active_platforms or "youtube" not in platform_results:
        return False
    unresolved = [platform for platform in active_platforms if platform not in platform_results]
    if not unresolved:
        return False
    if any(platform == "youtube" for platform in unresolved):
        return False
    return all(platform in {"instagram", "tiktok"} for platform in unresolved)


def post_reserved_queue_entry_to_platforms(entry, *, interactive_auth=False, platform_targets=None):
    platform_targets = dict(platform_targets or queue_platform_targets())
    active_platforms = [name for name in ("youtube", "instagram", "tiktok") if platform_targets.get(name)]
    if not active_platforms:
        raise RuntimeError("No enabled platforms were selected for this queued short.")

    caption = build_queue_shortform_caption(entry)
    working_entry = dict(entry)
    working_entry["platform_results"] = queue_entry_platform_results(entry)
    completed_results = dict(working_entry["platform_results"])
    errors = {}

    for platform in active_platforms:
        existing_result = (queue_entry_platform_results(working_entry).get(platform) or {})
        if queue_entry_platform_completed(working_entry, platform):
            if existing_result:
                completed_results[platform] = existing_result
            continue

        try:
            if platform == "youtube":
                result = upload_short_to_youtube(working_entry, interactive=interactive_auth)
            elif platform == "instagram":
                result = post_reel_to_instagram(
                    working_entry["file_path"],
                    caption,
                    existing_platform_result=existing_result,
                    progress_callback=(
                        lambda partial_result, queue_id=working_entry["queue_id"]: persist_queue_entry_platform_progress(
                            queue_id,
                            "instagram",
                            partial_result,
                        )
                    ),
                )
            else:
                result = post_video_to_tiktok(
                    working_entry["file_path"],
                    caption,
                    existing_platform_result=existing_result,
                    progress_callback=(
                        lambda partial_result, queue_id=working_entry["queue_id"]: persist_queue_entry_platform_progress(
                            queue_id,
                            "tiktok",
                            partial_result,
                        )
                    ),
                )
        except Exception as exc:
            if platform == "instagram":
                accepted_result = accept_buffer_instagram_result(
                    queue_entry_platform_results(working_entry).get("instagram") or existing_result,
                    followup_error=str(exc),
                )
                if accepted_result:
                    completed_results[platform] = dict(accepted_result)
                    persist_queue_entry_platform_result(working_entry["queue_id"], platform, accepted_result)
                    apply_queue_entry_platform_result(working_entry, platform, accepted_result)
                    continue
            if platform == "tiktok":
                accepted_result = accept_buffer_tiktok_result(
                    queue_entry_platform_results(working_entry).get("tiktok") or existing_result,
                    followup_error=str(exc),
                )
                if accepted_result:
                    completed_results[platform] = dict(accepted_result)
                    persist_queue_entry_platform_result(working_entry["queue_id"], platform, accepted_result)
                    apply_queue_entry_platform_result(working_entry, platform, accepted_result)
                    continue
            errors[platform] = str(exc)
            continue

        completed_results[platform] = dict(result)
        persist_queue_entry_platform_result(working_entry["queue_id"], platform, result)
        apply_queue_entry_platform_result(working_entry, platform, result)

    return {
        "active_platforms": active_platforms,
        "platform_results": completed_results,
        "platform_errors": errors,
    }


def remove_posted_clip_file(file_path):
    if not env_bool("VIDEO_DELETE_POSTED_SHORTS", DEFAULT_DELETE_POSTED_SHORTS):
        return {"deleted": False, "error": None, "skipped": True}

    deleted, error = unlink_if_exists(file_path)
    return {
        "deleted": deleted,
        "error": error,
        "skipped": False,
    }


def youtube_paths():
    client_secret = resolve_optional_path(
        os.environ.get("YOUTUBE_CLIENT_SECRET_FILE"),
        DEFAULT_CLIENT_SECRET_PATH,
        relative_base=SHORTS_DIR,
    )
    token_path = resolve_optional_path(
        os.environ.get("YOUTUBE_TOKEN_FILE"),
        DEFAULT_TOKEN_PATH,
        relative_base=SHORTS_DIR,
    )
    return client_secret, token_path


def build_youtube_service(interactive=False):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            "Missing Google API dependencies. Install requirements.txt before enabling the shorts poster."
        ) from exc

    client_secret_path, token_path = youtube_paths()
    if not client_secret_path.exists():
        raise RuntimeError(
            f"YouTube client secret not found: {client_secret_path}. "
            "Copy your OAuth client json there or set YOUTUBE_CLIENT_SECRET_FILE."
        )

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid:
        if not interactive:
            raise RuntimeError(
                f"No valid YouTube OAuth token found at {token_path}. "
                "Run shorts/post_next_short.py --authorize once on this machine."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), SCOPES)
        creds = flow.run_local_server(port=0)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    with open(token_path, "w", encoding="utf-8") as handle:
        handle.write(creds.to_json())

    return build("youtube", "v3", credentials=creds, cache_discovery=False), token_path


def authorize_youtube():
    _, token_path = build_youtube_service(interactive=True)
    return {"status": "authorized", "token_file": str(token_path)}


def upload_short_to_youtube(entry, interactive=False):
    try:
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise RuntimeError(
            "Missing Google API dependencies. Install requirements.txt before enabling the shorts poster."
        ) from exc

    service, _ = build_youtube_service(interactive=interactive)
    payload = build_entry_payload(entry)

    body = {
        "snippet": {
            "title": payload["title"],
            "description": payload["description"],
            "tags": payload["tags"],
            "categoryId": payload["category_id"],
        },
        "status": {
            "privacyStatus": payload["privacy_status"],
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(entry["file_path"], chunksize=-1, resumable=True, mimetype="video/mp4")
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        _, response = request.next_chunk()

    video_id = response["id"]
    return {
        "video_id": video_id,
        "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
        "title": payload["title"],
    }


def cleanup_orphan_clips(referenced_paths):
    ensure_layout()
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=env_int(
            "VIDEO_ORPHAN_CLIP_RETENTION_DAYS",
            DEFAULT_ORPHAN_CLIP_RETENTION_DAYS,
            minimum=0,
        )
    )
    deleted = 0
    freed_bytes = 0
    errors = []

    for candidate in CLIPS_DIR.iterdir():
        if not candidate.is_file() or candidate.name == ".gitkeep":
            continue
        if normalize_path(candidate) in referenced_paths:
            continue
        if path_timestamp(candidate) >= cutoff:
            continue

        file_size = path_size_bytes(candidate)
        ok, error = unlink_if_exists(candidate)
        if ok:
            deleted += 1
            freed_bytes += file_size
        elif error:
            errors.append(f"{candidate}: {error}")

    return {
        "deleted_orphan_clips": deleted,
        "freed_bytes": freed_bytes,
        "errors": errors,
    }


def cleanup_render_outputs():
    ensure_layout()
    keep_jobs = env_int("VIDEO_RENDER_KEEP_JOBS", DEFAULT_RENDER_KEEP_JOBS, minimum=0)
    retention_days = env_int("VIDEO_RENDER_RETENTION_DAYS", DEFAULT_RENDER_RETENTION_DAYS, minimum=0)
    retention_cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    min_age_cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=env_int(
            "VIDEO_RENDER_MIN_AGE_MINUTES",
            DEFAULT_RENDER_MIN_AGE_MINUTES,
            minimum=0,
        )
    )
    max_bytes = int(
        env_float("VIDEO_RENDER_MAX_GB", DEFAULT_RENDER_MAX_GB, minimum=0.0) * 1024 * 1024 * 1024
    )

    entries = []
    for candidate in OUT_DIR.iterdir():
        if not candidate.is_dir() or candidate.name in OUT_SNAPSHOT_NAMES:
            continue
        entries.append(
            {
                "path": candidate,
                "mtime": path_timestamp(candidate),
                "size": path_size_bytes(candidate),
            }
        )

    entries.sort(key=lambda item: item["mtime"], reverse=True)

    deleted_by_age = 0
    deleted_by_budget = 0
    freed_bytes = 0
    errors = []
    remaining = []

    for index, item in enumerate(entries):
        active_job = any((item["path"] / marker).exists() for marker in ACTIVE_RENDER_MARKERS)
        active_job = active_job or any((item["path"] / filename).exists() for filename in ACTIVE_RENDER_FILES)
        protected_recent = item["mtime"] >= min_age_cutoff
        protected_keep = keep_jobs > 0 and index < keep_jobs
        if active_job or protected_recent or protected_keep or item["mtime"] >= retention_cutoff:
            remaining.append(item)
            continue

        ok, error = rmtree_if_exists(item["path"])
        if ok:
            deleted_by_age += 1
            freed_bytes += item["size"]
        elif error:
            errors.append(f"{item['path']}: {error}")
            remaining.append(item)

    total_remaining_bytes = sum(item["size"] for item in remaining)
    if max_bytes > 0 and total_remaining_bytes > max_bytes:
        budget_candidates = sorted(
            (
                item
                for item in remaining
                if item["mtime"] < min_age_cutoff
                and not any((item["path"] / marker).exists() for marker in ACTIVE_RENDER_MARKERS)
                and not any((item["path"] / filename).exists() for filename in ACTIVE_RENDER_FILES)
            ),
            key=lambda item: item["mtime"],
        )
        deleted_paths = set()
        for item in budget_candidates:
            if total_remaining_bytes <= max_bytes:
                break
            ok, error = rmtree_if_exists(item["path"])
            if ok:
                deleted_paths.add(item["path"])
                deleted_by_budget += 1
                freed_bytes += item["size"]
                total_remaining_bytes -= item["size"]
            elif error:
                errors.append(f"{item['path']}: {error}")

        if deleted_paths:
            remaining = [item for item in remaining if item["path"] not in deleted_paths]

    return {
        "deleted_render_dirs_by_age": deleted_by_age,
        "deleted_render_dirs_by_budget": deleted_by_budget,
        "freed_bytes": freed_bytes,
        "remaining_render_dirs": len(remaining),
        "remaining_bytes": sum(item["size"] for item in remaining),
        "errors": errors,
    }


def run_storage_maintenance():
    ensure_layout()

    with queue_lock():
        queue = load_queue_unlocked()
        changed = release_stale_posting_items(queue)
        queue, removed_posted = prune_posted_queue_entries(queue)
        if removed_posted:
            changed = True
        if changed:
            save_queue_unlocked(queue)

        tracked_statuses = {"pending", "posting"}
        if not env_bool("VIDEO_DELETE_POSTED_SHORTS", DEFAULT_DELETE_POSTED_SHORTS):
            tracked_statuses.add("posted")
        referenced_paths = {
            normalize_path(item.get("file_path"))
            for item in queue
            if item.get("status") in tracked_statuses and item.get("file_path")
        }
        pending_count = sum(1 for item in queue if item.get("status") == "pending")
        posting_count = sum(1 for item in queue if item.get("status") == "posting")
        posted_count = sum(1 for item in queue if item.get("status") == "posted")

    clip_summary = cleanup_orphan_clips(referenced_paths)
    render_summary = cleanup_render_outputs()

    return {
        "status": "ok",
        "queue_entries_pruned": removed_posted,
        "pending_queue_items": pending_count,
        "posting_queue_items": posting_count,
        "posted_queue_items": posted_count,
        "orphan_clips_deleted": clip_summary["deleted_orphan_clips"],
        "render_dirs_deleted": (
            render_summary["deleted_render_dirs_by_age"] + render_summary["deleted_render_dirs_by_budget"]
        ),
        "freed_bytes": clip_summary["freed_bytes"] + render_summary["freed_bytes"],
        "errors": clip_summary["errors"] + render_summary["errors"],
    }


def post_next_queued_short(
    dry_run=False,
    interactive_auth=False,
    *,
    force_all_platforms=False,
    drop_from_queue_on_success=False,
):
    ensure_layout()
    entry = reserve_next_short()
    if entry is None:
        cleanup_summary = run_storage_maintenance()
        return {
            "status": "idle",
            "message": "No pending shorts in queue.",
            "cleanup": cleanup_summary,
        }

    if not os.path.exists(entry["file_path"]):
        error = f"queued short missing: {entry['file_path']}"
        mark_failed(entry["queue_id"], error)
        cleanup_summary = run_storage_maintenance()
        return {
            "status": "error",
            "message": error,
            "queue_id": entry["queue_id"],
            "cleanup": cleanup_summary,
        }

    if dry_run:
        release_reserved(entry["queue_id"])
        payload = build_entry_payload(entry)
        cleanup_summary = run_storage_maintenance()
        return {
            "status": "dry_run",
            "queue_id": entry["queue_id"],
            "file_path": entry["file_path"],
            "title": payload["title"],
            "description": payload["description"],
            "platform_targets": queue_platform_targets(force_all_platforms=force_all_platforms),
            "cleanup": cleanup_summary,
        }

    try:
        publish_result = post_reserved_queue_entry_to_platforms(
            entry,
            interactive_auth=interactive_auth,
            platform_targets=queue_platform_targets(force_all_platforms=force_all_platforms),
        )
    except KeyboardInterrupt:
        release_reserved(entry["queue_id"])
        cleanup_summary = run_storage_maintenance()
        return {
            "status": "interrupted",
            "queue_id": entry["queue_id"],
            "message": "Posting was interrupted before completion.",
            "cleanup": cleanup_summary,
        }
    except Exception as exc:
        mark_failed(entry["queue_id"], str(exc))
        cleanup_summary = run_storage_maintenance()
        return {
            "status": "error",
            "queue_id": entry["queue_id"],
            "message": str(exc),
            "cleanup": cleanup_summary,
        }

    platform_errors = publish_result["platform_errors"]
    platform_results = publish_result["platform_results"]
    active_platforms = publish_result["active_platforms"]
    completed_platforms = [platform for platform in active_platforms if platform in platform_results]

    if platform_errors or len(completed_platforms) != len(active_platforms):
        message = queue_platform_error_message(platform_errors) or "One or more platform posts did not complete."
        if should_finalize_queue_entry_after_social_failures(active_platforms, platform_results, platform_errors):
            mark_posted_partial(
                entry["queue_id"],
                platform_results=platform_results,
                platform_errors=platform_errors,
                file_cleanup={"deleted": False, "error": None, "skipped": True},
            )
            cleanup_summary = run_storage_maintenance()
            return {
                "status": "partial_posted",
                "queue_id": entry["queue_id"],
                "message": message,
                "active_platforms": active_platforms,
                "completed_platforms": completed_platforms,
                "platform_results": platform_results,
                "platform_errors": platform_errors,
                "queue_deleted": False,
                "local_file_deleted": False,
                "local_file_delete_error": None,
                "cleanup": cleanup_summary,
            }
        mark_failed(entry["queue_id"], message)
        cleanup_summary = run_storage_maintenance()
        return {
            "status": "error",
            "queue_id": entry["queue_id"],
            "message": message,
            "active_platforms": active_platforms,
            "completed_platforms": completed_platforms,
            "platform_results": platform_results,
            "platform_errors": platform_errors,
            "cleanup": cleanup_summary,
        }

    file_cleanup = remove_posted_clip_file(entry["file_path"])
    youtube_result = platform_results.get("youtube") or {}
    mark_posted(
        entry["queue_id"],
        youtube_result.get("video_id"),
        youtube_result.get("youtube_url"),
        file_cleanup=file_cleanup,
        platform_results=platform_results,
    )
    queue_deleted = delete_queue_entry(entry["queue_id"]) if drop_from_queue_on_success else False
    cleanup_summary = run_storage_maintenance()
    return {
        "status": "posted",
        "queue_id": entry["queue_id"],
        "video_id": youtube_result.get("video_id"),
        "youtube_url": youtube_result.get("youtube_url"),
        "title": youtube_result.get("title") or entry.get("title"),
        "file_path": entry["file_path"],
        "active_platforms": active_platforms,
        "platform_results": platform_results,
        "queue_deleted": queue_deleted,
        "local_file_deleted": file_cleanup["deleted"],
        "local_file_delete_error": file_cleanup["error"],
        "cleanup": cleanup_summary,
    }


def post_next_queued_short_to_all_platforms(
    dry_run=False,
    interactive_auth=False,
    *,
    delete_queue_entry_on_success=True,
):
    return post_next_queued_short(
        dry_run=dry_run,
        interactive_auth=interactive_auth,
        force_all_platforms=True,
        drop_from_queue_on_success=delete_queue_entry_on_success,
    )


def require_requests():
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError(
            "Missing HTTP client dependency. Install requirements.txt before using Instagram or TikTok posting."
        ) from exc
    return requests


def http_json(method, url, *, headers=None, params=None, payload=None, timeout=60):
    requests = require_requests()
    response = requests.request(
        method,
        url,
        headers=headers,
        params=params,
        json=payload,
        timeout=timeout,
    )
    if response.status_code >= 400:
        detail = response.text.strip()
        raise RuntimeError(f"HTTP {response.status_code} from {url}: {detail[:500]}")
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"Non-JSON response from {url}: {response.text[:500]}") from exc


def normalize_instagram_publish_method(value):
    method = (value or "").strip().lower()
    if not method:
        return DEFAULT_INSTAGRAM_PUBLISH_METHOD
    if method not in {"quick_tunnel", "resumable"}:
        raise RuntimeError(
            f"Unsupported Instagram publish method {value!r}. Use 'quick_tunnel' or 'resumable'."
        )
    return method


def normalize_instagram_publish_backend(value):
    backend = (value or "").strip().lower()
    if not backend:
        return DEFAULT_INSTAGRAM_PUBLISH_BACKEND
    if backend not in {"native", "buffer"}:
        raise RuntimeError(
            f"Unsupported Instagram publish backend {value!r}. Use 'native' or 'buffer'."
        )
    return backend


def normalize_tiktok_publish_backend(value):
    backend = (value or "").strip().lower()
    if not backend:
        return DEFAULT_TIKTOK_PUBLISH_BACKEND
    if backend not in {"native", "buffer"}:
        raise RuntimeError(
            f"Unsupported TikTok publish backend {value!r}. Use 'native' or 'buffer'."
        )
    return backend


def graph_api_headers(access_token):
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }


def instagram_graph_headers():
    return {"Content-Type": "application/json"}


def instagram_appsecret_proof(access_token, app_secret):
    if not access_token or not app_secret:
        return ""
    digest = hmac.new(
        app_secret.encode("utf-8"),
        access_token.encode("utf-8"),
        hashlib.sha256,
    )
    return digest.hexdigest()


def instagram_graph_params(settings, extra=None):
    params = dict(extra or {})
    access_token = (settings.get("access_token") or "").strip()
    if access_token:
        params["access_token"] = access_token
    proof = instagram_appsecret_proof(settings.get("access_token"), settings.get("app_secret"))
    if proof:
        params["appsecret_proof"] = proof
    return params


def raise_instagram_error(data, default_message):
    error = (data or {}).get("error") or {}
    if not error:
        return
    message = error.get("message") or default_message
    code = error.get("code")
    subcode = error.get("error_subcode")
    parts = [message]
    if code is not None:
        parts.append(f"code={code}")
    if subcode is not None:
        parts.append(f"subcode={subcode}")
    raise RuntimeError(" | ".join(parts))


def raise_tiktok_error(data, default_message):
    error = (data or {}).get("error") or {}
    code = error.get("code")
    if not error or code in {None, "", "ok"}:
        return
    message = error.get("message") or default_message
    raise RuntimeError(f"{message} | code={code}")


def instagram_settings():
    raw_backend = (os.environ.get("INSTAGRAM_PUBLISH_BACKEND") or "").strip()
    buffer_api_key = (os.environ.get("BUFFER_API_KEY") or "").strip()
    publish_backend = normalize_instagram_publish_backend(raw_backend)
    if not raw_backend and buffer_api_key:
        publish_backend = "buffer"
    access_token = (os.environ.get("INSTAGRAM_ACCESS_TOKEN") or "").strip()
    ig_user_id = (os.environ.get("INSTAGRAM_IG_USER_ID") or "").strip()
    app_secret = (os.environ.get("INSTAGRAM_APP_SECRET") or "").strip()
    publish_method = normalize_instagram_publish_method(os.environ.get("INSTAGRAM_PUBLISH_METHOD"))
    api_version = (os.environ.get("INSTAGRAM_API_VERSION") or DEFAULT_INSTAGRAM_API_VERSION).strip()
    default_graph_host = "graph.instagram.com" if publish_method == "quick_tunnel" else "graph.facebook.com"
    graph_host = (os.environ.get("INSTAGRAM_GRAPH_HOST") or default_graph_host).strip()
    if not api_version:
        api_version = DEFAULT_INSTAGRAM_API_VERSION
    if not graph_host:
        graph_host = default_graph_host
    if publish_backend != "buffer" and not access_token:
        raise RuntimeError(
            "Missing INSTAGRAM_ACCESS_TOKEN. Copy shorts/instagram.env.example to shorts/instagram.env and fill it in."
        )
    if publish_backend != "buffer" and not ig_user_id:
        raise RuntimeError(
            "Missing INSTAGRAM_IG_USER_ID. Copy shorts/instagram.env.example to shorts/instagram.env and fill it in."
        )
    return {
        "publish_backend": publish_backend,
        "access_token": access_token,
        "ig_user_id": ig_user_id,
        "app_secret": app_secret,
        "publish_method": publish_method,
        "api_version": api_version,
        "graph_host": graph_host,
        "cta_text": (os.environ.get("INSTAGRAM_CTA_TEXT") or DEFAULT_INSTAGRAM_CTA_TEXT).strip(),
        "share_to_feed": env_bool("INSTAGRAM_SHARE_TO_FEED", True),
        "cloudflared_bin": (os.environ.get("INSTAGRAM_CLOUDFLARED_BIN") or "cloudflared").strip() or "cloudflared",
        "poll_seconds": env_int("INSTAGRAM_POLL_SECONDS", DEFAULT_INSTAGRAM_POLL_SECONDS, minimum=1),
        "poll_timeout_seconds": env_int(
            "INSTAGRAM_POLL_TIMEOUT_SECONDS",
            DEFAULT_INSTAGRAM_POLL_TIMEOUT_SECONDS,
            minimum=5,
        ),
        "quick_tunnel_timeout_seconds": env_int(
            "INSTAGRAM_QUICK_TUNNEL_TIMEOUT_SECONDS",
            DEFAULT_INSTAGRAM_QUICK_TUNNEL_TIMEOUT_SECONDS,
            minimum=5,
        ),
        "quick_tunnel_grace_seconds": env_int(
            "INSTAGRAM_QUICK_TUNNEL_GRACE_SECONDS",
            DEFAULT_INSTAGRAM_QUICK_TUNNEL_GRACE_SECONDS,
            minimum=0,
        ),
        "quick_tunnel_min_interval_seconds": env_int(
            "INSTAGRAM_QUICK_TUNNEL_MIN_INTERVAL_SECONDS",
            DEFAULT_QUICK_TUNNEL_MIN_INTERVAL_SECONDS,
            minimum=0,
        ),
        "quick_tunnel_max_attempts": env_int(
            "INSTAGRAM_QUICK_TUNNEL_MAX_ATTEMPTS",
            DEFAULT_QUICK_TUNNEL_MAX_ATTEMPTS,
            minimum=1,
        ),
        "quick_tunnel_retry_delay_seconds": env_int(
            "INSTAGRAM_QUICK_TUNNEL_RETRY_DELAY_SECONDS",
            DEFAULT_QUICK_TUNNEL_RETRY_DELAY_SECONDS,
            minimum=1,
        ),
        "quick_tunnel_doh_url": (
            os.environ.get("INSTAGRAM_QUICK_TUNNEL_DOH_URL") or DEFAULT_QUICK_TUNNEL_DOH_URL
        ).strip(),
        "quick_tunnel_settle_seconds": env_int(
            "INSTAGRAM_QUICK_TUNNEL_SETTLE_SECONDS",
            DEFAULT_QUICK_TUNNEL_SETTLE_SECONDS,
            minimum=0,
        ),
    }


def get_instagram_container_status(container_id, settings=None):
    settings = settings or instagram_settings()
    url = f"https://{settings['graph_host']}/{settings['api_version']}/{container_id}"
    data = http_json(
        "GET",
        url,
        headers=instagram_graph_headers(),
        params=instagram_graph_params(settings, {"fields": "status_code,status"}),
    )
    raise_instagram_error(data, "Instagram container status lookup failed.")
    return {
        "status_code": (data.get("status_code") or data.get("status") or "").strip().upper(),
        "raw": data,
    }


def get_instagram_container_status_details(container_id, settings=None):
    settings = settings or instagram_settings()
    url = f"https://{settings['graph_host']}/{settings['api_version']}/{container_id}"
    data = http_json(
        "GET",
        url,
        headers=instagram_graph_headers(),
        params=instagram_graph_params(
            settings,
            {"fields": "status_code,status,video_status,error_type,error_message"},
        ),
    )
    raise_instagram_error(data, "Instagram container extended status lookup failed.")
    return data


def check_instagram_token_valid(settings):
    url = f"https://{settings['graph_host']}/{settings['api_version']}/me"
    requests = require_requests()
    try:
        response = requests.get(
            url,
            headers=instagram_graph_headers(),
            params=instagram_graph_params(settings, {"fields": "id,name"}),
            timeout=30,
        )
    except Exception as exc:
        warning = f"Instagram token health check skipped: {exc}"
        print(f"warning: {warning}", file=sys.stderr)
        return {
            "status": "warning",
            "warning": warning,
            "graph_host": settings["graph_host"],
            "api_version": settings["api_version"],
        }

    data = {}
    if response.content:
        try:
            data = response.json()
        except ValueError:
            data = {}
    error = (data or {}).get("error") or {}
    error_code = error.get("code")

    if 400 <= response.status_code < 500 or error_code == 190:
        raise RuntimeError(
            "Instagram access token is invalid or expired "
            f"(code={error_code or 190}). Refresh your long-lived token and update "
            "INSTAGRAM_ACCESS_TOKEN in shorts/instagram.env before retrying."
        )

    if response.status_code >= 500 or error:
        detail = error.get("message") or response.text.strip()[:300] or f"HTTP {response.status_code}"
        warning = f"Instagram token health check skipped: {detail}"
        print(f"warning: {warning}", file=sys.stderr)
        return {
            "status": "warning",
            "warning": warning,
            "graph_host": settings["graph_host"],
            "api_version": settings["api_version"],
        }

    return {
        "status": "ok",
        "graph_host": settings["graph_host"],
        "api_version": settings["api_version"],
        "id": data.get("id"),
        "name": data.get("name"),
    }


def wait_for_instagram_container(container_id, settings=None):
    settings = settings or instagram_settings()
    deadline = time.time() + settings["poll_timeout_seconds"]
    last_snapshot = None

    while time.time() <= deadline:
        snapshot = get_instagram_container_status(container_id, settings=settings)
        last_snapshot = snapshot
        status_code = snapshot["status_code"]
        if status_code == "FINISHED":
            return snapshot
        if status_code in {"ERROR", "EXPIRED"}:
            detail_snapshot = None
            try:
                detail_snapshot = get_instagram_container_status_details(container_id, settings=settings)
            except Exception:
                detail_snapshot = None
            raise RuntimeError(
                f"Instagram container {container_id} failed with status {status_code}. "
                f"Snapshot: {json.dumps(snapshot['raw'], sort_keys=True)}. "
                f"Detail: {json.dumps(detail_snapshot or snapshot['raw'], sort_keys=True)}"
            )
        time.sleep(settings["poll_seconds"])

    last_code = (last_snapshot or {}).get("status_code") or "UNKNOWN"
    raise RuntimeError(
        f"Timed out waiting for Instagram container {container_id} to finish. Last status: {last_code}."
    )


def probe_media_file(path, *, ffprobe_bin=None):
    ffprobe_bin = ffprobe_bin or shutil.which("ffprobe")
    if not ffprobe_bin:
        raise RuntimeError("ffprobe is required to inspect Instagram uploads.")
    result = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Instagram upload probe failed: {result.stderr[-500:]}")
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("Instagram upload probe returned invalid JSON.") from exc


def ffprobe_rate_to_fps(rate):
    raw = (rate or "").strip()
    if not raw or raw in {"0/0", "N/A"}:
        return 0.0
    if "/" in raw:
        numerator, denominator = raw.split("/", 1)
        try:
            denominator_value = float(denominator)
            if denominator_value == 0:
                return 0.0
            return float(numerator) / denominator_value
        except ValueError:
            return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def format_debug_fps(fps):
    if fps <= 0:
        return "unknown"
    rounded = round(fps)
    if abs(fps - rounded) < 0.05:
        return str(int(rounded))
    return f"{fps:.2f}".rstrip("0").rstrip(".")


def fetch_instagram_media_details(media_id, settings=None):
    settings = settings or instagram_settings()
    url = f"https://{settings['graph_host']}/{settings['api_version']}/{media_id}"
    data = http_json(
        "GET",
        url,
        headers=instagram_graph_headers(),
        params=instagram_graph_params(
            settings,
            {"fields": "id,media_type,media_product_type,permalink"},
        ),
    )
    raise_instagram_error(data, "Instagram media lookup failed.")
    return data


def parse_http_byte_range(header_value, total_size):
    if total_size < 1:
        return None
    raw = (header_value or "").strip()
    if not raw.startswith("bytes="):
        return None
    spec = raw[len("bytes=") :].strip()
    if not spec or "," in spec or "-" not in spec:
        return None

    start_text, end_text = spec.split("-", 1)
    try:
        if start_text:
            start = int(start_text)
            end = int(end_text) if end_text else total_size - 1
        else:
            suffix_size = int(end_text)
            if suffix_size <= 0:
                return None
            start = max(total_size - suffix_size, 0)
            end = total_size - 1
    except ValueError:
        return None

    if start < 0 or start >= total_size:
        return None
    end = min(end, total_size - 1)
    if end < start:
        return None
    return start, end


@contextmanager
def serve_file_over_http(file_path):
    target = Path(file_path).expanduser().resolve()
    if not target.exists():
        raise FileNotFoundError(target)

    route = f"/{uuid.uuid4().hex}{target.suffix or '.bin'}"
    content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"

    class SingleFileRequestHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self._serve(send_body=True)

        def do_HEAD(self):
            self._serve(send_body=False)

        def log_message(self, format_text, *args):
            return

        def _serve(self, *, send_body):
            request_path = urlsplit(self.path).path
            if request_path != route:
                print(
                    "temporary video server: "
                    f"{self.command} {request_path} -> 404 expected={route}",
                    file=sys.stderr,
                )
                self.send_error(404)
                return

            try:
                total_size = target.stat().st_size
            except OSError:
                self.send_error(404)
                return

            start = 0
            end = max(total_size - 1, 0)
            partial = False

            range_header = self.headers.get("Range")
            if range_header:
                byte_range = parse_http_byte_range(range_header, total_size)
                if byte_range is None:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total_size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start, end = byte_range
                partial = True

            content_length = max(end - start + 1, 0)
            self.send_response(206 if partial else 200)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "public, max-age=600")
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", f'inline; filename="{target.name}"')
            self.send_header("Content-Length", str(content_length))
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{total_size}")
            self.end_headers()

            status_code = 206 if partial else 200
            print(
                "temporary video server: "
                f"{self.command} {request_path} -> {status_code} "
                f"range={range_header or '-'} bytes={content_length}/{total_size}",
                file=sys.stderr,
            )

            if not send_body or content_length <= 0:
                return

            bytes_sent = 0
            with open(target, "rb") as handle:
                handle.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        # Buffer and other fetchers may close the socket as soon as they have what they need.
                        print(
                            "temporary video server: "
                            f"{self.command} {request_path} disconnected after {bytes_sent}/{content_length} bytes",
                            file=sys.stderr,
                        )
                        return
                    bytes_sent += len(chunk)
                    remaining -= len(chunk)
            print(
                "temporary video server: "
                f"{self.command} {request_path} completed {bytes_sent}/{content_length} bytes",
                file=sys.stderr,
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), SingleFileRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    local_origin = f"http://127.0.0.1:{server.server_address[1]}"
    local_url = f"{local_origin}{route}"

    try:
        yield {
            "file_path": str(target),
            "local_origin": local_origin,
            "local_url": local_url,
            "route": route,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def terminate_subprocess(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def quick_tunnel_config_conflict():
    config_dir = Path.home() / ".cloudflared"
    for filename in ("config.yml", "config.yaml"):
        candidate = config_dir / filename
        if candidate.exists():
            return candidate
    return None


def wait_for_trycloudflare_url(process, timeout_seconds):
    if process.stdout is None:
        raise RuntimeError("cloudflared did not expose stdout for quick tunnel startup.")

    lines = queue.Queue()
    pattern = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
    recent_output = []

    def pump_output():
        for raw_line in process.stdout:
            lines.put(raw_line.rstrip())
        lines.put(None)

    reader = threading.Thread(target=pump_output, daemon=True)
    reader.start()

    deadline = time.time() + timeout_seconds
    while time.time() <= deadline:
        if process.poll() is not None and lines.empty():
            break
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            continue
        if line is None:
            continue
        if line:
            recent_output.append(line)
            recent_output = recent_output[-20:]
        match = pattern.search(line or "")
        if match:
            return match.group(0)

    details = "\n".join(recent_output[-10:]).strip() or "(no tunnel logs captured)"
    if process.poll() is not None:
        raise RuntimeError(f"cloudflared exited before creating a quick tunnel.\n{details}")
    raise RuntimeError(f"Timed out waiting for cloudflared to create a quick tunnel.\n{details}")


def load_json_or_default(path, default):
    candidate = Path(path)
    if not candidate.exists():
        return default
    try:
        with open(candidate, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


@contextmanager
def quick_tunnel_slot(settings=None):
    ensure_layout()
    settings = settings or instagram_settings()
    min_interval = max(int(settings.get("quick_tunnel_min_interval_seconds") or 0), 0)

    with open(QUICK_TUNNEL_LOCK_PATH, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            state = load_json_or_default(QUICK_TUNNEL_STATE_PATH, {})
            last_started_at = float(state.get("last_started_at") or 0)
            wait_seconds = (last_started_at + min_interval) - time.time()
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            atomic_write_json(
                QUICK_TUNNEL_STATE_PATH,
                {"last_started_at": time.time()},
            )
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def cloudflare_quick_tunnel(local_url, settings=None):
    settings = settings or instagram_settings()
    cloudflared_bin = settings.get("cloudflared_bin") or "cloudflared"
    binary_path = shutil.which(cloudflared_bin)
    if not binary_path:
        raise RuntimeError(
            f"Could not find {cloudflared_bin!r}. Install cloudflared or set INSTAGRAM_CLOUDFLARED_BIN."
        )

    conflict = quick_tunnel_config_conflict()
    if conflict is not None:
        raise RuntimeError(
            "Cloudflare quick tunnels do not run when a local cloudflared config file is present. "
            f"Temporarily rename {conflict} or switch INSTAGRAM_PUBLISH_METHOD=resumable."
        )

    with quick_tunnel_slot(settings=settings):
        process = subprocess.Popen(
            [binary_path, "tunnel", "--url", local_url],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )

        try:
            public_origin = wait_for_trycloudflare_url(process, settings["quick_tunnel_timeout_seconds"])
            yield {
                "public_origin": public_origin,
            }
        finally:
            terminate_subprocess(process)


@contextmanager
def temporary_instagram_video_url(file_path, settings=None):
    settings = settings or instagram_settings()
    max_attempts = max(int(settings.get("quick_tunnel_max_attempts") or 1), 1)
    retry_delay_seconds = max(int(settings.get("quick_tunnel_retry_delay_seconds") or 1), 1)
    last_error = None

    for attempt in range(1, max_attempts + 1):
        active_stack = ExitStack()
        try:
            local_server = active_stack.enter_context(serve_file_over_http(file_path))
            tunnel = active_stack.enter_context(
                cloudflare_quick_tunnel(local_server["local_origin"], settings=settings)
            )
            public_url = f"{tunnel['public_origin']}{local_server['route']}"
            settle_seconds = max(int(settings.get("quick_tunnel_settle_seconds") or 0), 0)
            if settle_seconds > 0:
                time.sleep(settle_seconds)
            probe = wait_for_public_video_url(
                public_url,
                timeout_seconds=max(settings["quick_tunnel_grace_seconds"], 0),
                required=True,
                doh_url=(settings.get("quick_tunnel_doh_url") or "").strip(),
            )
            hosted_video = {
                "local_url": local_server["local_url"],
                "public_origin": tunnel["public_origin"],
                "public_url": public_url,
                "probe": probe,
            }
        except Exception as exc:
            active_stack.close()
            last_error = exc
            if attempt >= max_attempts:
                break
            time.sleep(retry_delay_seconds * attempt)
            continue

        with active_stack:
            yield hosted_video
        return

    raise RuntimeError(
        f"Cloudflare quick tunnel failed after {max_attempts} attempts. Last error: {last_error}"
    ) from last_error


def build_instagram_reel_payload(
    *,
    share_to_feed,
    caption="",
    thumb_offset_ms=None,
    cover_url="",
    audio_name="",
    video_url="",
    upload_type="",
):
    payload = {
        "media_type": "REELS",
        "share_to_feed": bool(share_to_feed),
    }
    if video_url:
        payload["video_url"] = video_url
    if upload_type:
        payload["upload_type"] = upload_type
    if caption:
        payload["caption"] = caption
    if thumb_offset_ms is not None:
        payload["thumb_offset"] = int(thumb_offset_ms)
    if cover_url:
        payload["cover_url"] = cover_url
    if audio_name:
        payload["audio_name"] = audio_name
    return payload


def instagram_hashtags():
    youtube_defaults = sanitize_hashtags(_split_env_list("YOUTUBE_HASHTAGS", DEFAULT_SHORTFORM_HASHTAGS))
    return sanitize_hashtags(_split_env_list("INSTAGRAM_HASHTAGS", youtube_defaults))


def collect_caption_hashtags(text):
    seen_hashtags = set()
    for token in (text or "").replace("\n", " ").split():
        if token.startswith("#"):
            seen_hashtags.add(token.rstrip(".,!?;:").lower())
    return seen_hashtags


def build_shortform_caption(base_caption, *, description_text="", cta_text="", hashtags=None):
    hashtags = list(hashtags or [])
    base_text = (base_caption or "").strip()
    caption_blocks = []
    seen_hashtags = collect_caption_hashtags(base_text)

    if base_text:
        caption_blocks.append(base_text)

    description_value = (description_text or "").strip()
    if description_value and description_value.lower() not in base_text.lower():
        caption_blocks.append(description_value)

    cta_value = (cta_text or "").strip()
    if cta_value and cta_value.lower() not in base_text.lower():
        caption_blocks.append(cta_value)

    missing_hashtags = [tag for tag in hashtags if tag.lower() not in seen_hashtags]
    if missing_hashtags:
        caption_blocks.append(" ".join(missing_hashtags))

    return "\n\n".join(block for block in caption_blocks if block).strip()


def build_instagram_caption(caption="", settings=None):
    settings = settings or instagram_settings()
    return build_shortform_caption(
        caption,
        cta_text=settings.get("cta_text"),
        hashtags=instagram_hashtags(),
    )


def probe_public_video_url_with_curl(url, *, doh_url=""):
    curl_bin = shutil.which("curl")
    if not curl_bin:
        return {
            "reachable": False,
            "last_error": "curl not found",
            "method": "curl_doh" if doh_url else "curl",
        }

    cmd = [
        curl_bin,
        "--silent",
        "--show-error",
        "--output",
        os.devnull,
        "--range",
        "0-15",
        "--write-out",
        "%{http_code}",
    ]
    if doh_url:
        cmd.extend(["--doh-url", doh_url])
    cmd.append(url)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return {
            "reachable": False,
            "last_error": "curl timed out",
            "method": "curl_doh" if doh_url else "curl",
        }

    status_code = (result.stdout or "").strip()
    if result.returncode == 0 and status_code in {"200", "206"}:
        return {
            "reachable": True,
            "status_code": int(status_code),
            "method": "curl_doh" if doh_url else "curl",
        }

    stderr = (result.stderr or "").strip()
    detail = stderr or f"curl exit {result.returncode}, http {status_code or 'unknown'}"
    return {
        "reachable": False,
        "last_error": detail,
        "method": "curl_doh" if doh_url else "curl",
    }


def wait_for_public_video_url(url, timeout_seconds=20, *, required=True, doh_url=""):
    requests = require_requests()
    deadline = time.time() + timeout_seconds
    last_error = ""

    while time.time() <= deadline:
        curl_probe = probe_public_video_url_with_curl(url)
        if curl_probe.get("reachable"):
            return curl_probe
        last_error = f"{curl_probe.get('method')}: {curl_probe.get('last_error')}"

        if doh_url:
            curl_doh_probe = probe_public_video_url_with_curl(url, doh_url=doh_url)
            if curl_doh_probe.get("reachable"):
                return curl_doh_probe
            last_error = (
                f"{last_error}; {curl_doh_probe.get('method')}: {curl_doh_probe.get('last_error')}"
            )

        try:
            response = requests.get(
                url,
                headers={"Range": "bytes=0-15"},
                timeout=15,
            )
            if response.status_code in {200, 206} and response.content:
                return {
                    "reachable": True,
                    "status_code": response.status_code,
                    "content_length": response.headers.get("Content-Length"),
                    "method": "requests",
                }
            last_error = f"HTTP {response.status_code}"
        except requests.RequestException as exc:
            last_error = str(exc)
        time.sleep(1)

    if required:
        raise RuntimeError(
            f"Timed out waiting for public video URL {url} to become reachable. Last error: {last_error}"
        )
    return {
        "reachable": False,
        "last_error": last_error,
        "method": "requests",
    }


def create_instagram_media_container(payload, settings=None):
    settings = settings or instagram_settings()
    create_url = f"https://{settings['graph_host']}/{settings['api_version']}/{settings['ig_user_id']}/media"
    create_response = http_json(
        "POST",
        create_url,
        headers=instagram_graph_headers(),
        params=instagram_graph_params(settings),
        payload=payload,
    )
    raise_instagram_error(create_response, "Instagram container creation failed.")

    container_id = (create_response.get("id") or "").strip()
    if not container_id:
        raise RuntimeError(f"Instagram container creation returned no id: {create_response}")
    return create_response, container_id


def upload_instagram_reel_file(container_id, file_path, settings=None, upload_url=""):
    settings = settings or instagram_settings()
    target = Path(file_path).expanduser().resolve()
    effective_upload_url = upload_url or f"https://rupload.facebook.com/ig-api-upload/{settings['api_version']}/{container_id}"

    requests = require_requests()
    file_size = target.stat().st_size
    with open(target, "rb") as handle:
        upload_response = requests.post(
            effective_upload_url,
            headers={
                "Authorization": f"OAuth {settings['access_token']}",
                "offset": "0",
                "file_size": str(file_size),
            },
            data=handle,
            timeout=600,
        )

    if upload_response.status_code >= 400:
        raise RuntimeError(
            f"Instagram upload failed with HTTP {upload_response.status_code}: {upload_response.text[:500]}"
        )

    return effective_upload_url


def post_reel_to_instagram_via_buffer(
    file_path,
    caption="",
    *,
    share_to_feed=None,
    wait_for_finish=True,
    existing_platform_result=None,
    progress_callback=None,
):
    if not wait_for_finish:
        raise RuntimeError(
            "Buffer-backed Instagram publishing requires waiting so the temporary public video URL stays online "
            "until Buffer finishes fetching and sending the video."
        )

    settings = instagram_settings()
    buffer = buffer_settings()
    target = Path(file_path).expanduser().resolve()
    if not target.exists():
        raise FileNotFoundError(target)

    instagram_caption = build_instagram_caption(caption, settings=settings)
    share_value = settings["share_to_feed"] if share_to_feed is None else bool(share_to_feed)
    channel = query_buffer_instagram_channel(settings=buffer)
    existing_result = dict(existing_platform_result or {})
    existing_buffer_post_id = (existing_result.get("buffer_post_id") or "").strip()

    if existing_buffer_post_id:
        return accept_buffer_instagram_result(existing_result)

    with sanitized_instagram_upload_file(target) as upload_target:
        with temporary_buffer_video_url(upload_target, settings=buffer) as hosted_video:
            create_post_input = {
                "text": instagram_caption,
                "channelId": channel["id"],
                "schedulingType": "automatic",
                "mode": "shareNow",
                "source": "video_worker_instagram_buffer",
                "metadata": {
                    "instagram": {
                        "type": "reel",
                        "shouldShareToFeed": share_value,
                    }
                },
                "assets": {
                    "videos": [
                        {
                            "url": hosted_video["public_url"],
                        }
                    ]
                },
            }
            create_post_input.update(buffer_instagram_reel_input_fields(settings=buffer))
            created = buffer_graphql(
                buffer,
                """
                mutation CreatePost($input: CreatePostInput!) {
                  createPost(input: $input) {
                    __typename
                    ... on PostActionSuccess {
                      post {
                        id
                        status
                        text
                        shareMode
                        schedulingType
                        sharedNow
                        sentAt
                        assets {
                          id
                          mimeType
                          source
                        }
                      }
                    }
                    ... on MutationError {
                      message
                    }
                  }
                }
                """,
                variables={
                    "input": create_post_input,
                },
                default_message="Buffer Instagram createPost failed.",
            ).get("createPost") or {}

            if created.get("__typename") != "PostActionSuccess":
                message = (created.get("message") or "Buffer Instagram createPost failed.").strip()
                raise RuntimeError(message)

            post_snapshot = created.get("post") or {}
            print(
                "Buffer Instagram createPost snapshot: "
                f"{json.dumps(post_snapshot, sort_keys=True, default=str)}",
                file=sys.stderr,
            )
            accepted_result = accept_buffer_instagram_result(
                build_buffer_instagram_result(
                    post_snapshot,
                    channel,
                    target,
                    instagram_caption,
                    hosted_video_url=hosted_video["public_url"],
                )
            )
            accepted_result["share_to_feed"] = share_value
            if progress_callback and accepted_result:
                progress_callback(accepted_result)

            hold_seconds = int(buffer.get("tunnel_hold_seconds") or 0)
            hold_buffer_tunnel_for_fetch("Instagram", hosted_video, hold_seconds)

            return accepted_result


def post_reel_to_instagram(
    file_path,
    caption="",
    *,
    share_to_feed=None,
    thumb_offset_ms=None,
    cover_url="",
    audio_name="",
    wait_for_finish=True,
    publish_method=None,
    existing_platform_result=None,
    progress_callback=None,
):
    settings = instagram_settings()
    target = Path(file_path).expanduser().resolve()
    if not target.exists():
        raise FileNotFoundError(target)

    if settings["publish_backend"] == "buffer":
        if thumb_offset_ms is not None or cover_url or audio_name:
            raise RuntimeError(
                "The Buffer Instagram backend currently supports caption + video delivery only. "
                "Remove --thumb-offset-ms, --cover-url, and --audio-name."
            )
        return post_reel_to_instagram_via_buffer(
            target,
            caption,
            share_to_feed=share_to_feed,
            wait_for_finish=wait_for_finish,
            existing_platform_result=existing_platform_result,
            progress_callback=progress_callback,
        )

    method = normalize_instagram_publish_method(publish_method or settings["publish_method"])
    settings = dict(settings)
    settings["publish_method"] = method
    if not (os.environ.get("INSTAGRAM_GRAPH_HOST") or "").strip():
        settings["graph_host"] = "graph.instagram.com" if method == "quick_tunnel" else "graph.facebook.com"
    check_instagram_token_valid(settings)
    instagram_caption = build_instagram_caption(caption, settings=settings)
    share_value = settings["share_to_feed"] if share_to_feed is None else bool(share_to_feed)
    if method == "quick_tunnel" and not wait_for_finish:
        raise RuntimeError(
            "Quick tunnel publishing requires waiting for Meta processing so the temporary video URL stays online."
        )

    def publish_from_payload(payload, upload_target, *, hosted_video_url="", hosted_video_probe=None):
        create_response, container_id = create_instagram_media_container(payload, settings=settings)

        if method == "resumable":
            upload_instagram_reel_file(
                container_id,
                upload_target,
                settings=settings,
                upload_url=(create_response.get("uri") or "").strip(),
            )

        try:
            status_snapshot = get_instagram_container_status(container_id, settings=settings)
            if wait_for_finish:
                status_snapshot = wait_for_instagram_container(container_id, settings=settings)
        except Exception as exc:
            context = {
                "container_id": container_id,
                "graph_host": settings["graph_host"],
                "publish_method": method,
                "hosted_video_url": hosted_video_url or None,
                "hosted_video_probe": hosted_video_probe,
                "create_response": create_response,
                "payload_video_url": payload.get("video_url"),
                "payload_upload_type": payload.get("upload_type"),
            }
            raise RuntimeError(
                "Instagram processing failed before media_publish. "
                f"{exc}. Context: {json.dumps(context, sort_keys=True, default=str)}"
            ) from exc

        publish_url = (
            f"https://{settings['graph_host']}/{settings['api_version']}/{settings['ig_user_id']}/media_publish"
        )
        publish_response = http_json(
            "POST",
            publish_url,
            headers=instagram_graph_headers(),
            params=instagram_graph_params(settings),
            payload={"creation_id": container_id},
        )
        raise_instagram_error(publish_response, "Instagram media_publish failed.")

        media_id = (publish_response.get("id") or "").strip()
        media_details = fetch_instagram_media_details(media_id, settings=settings) if media_id else {}

        return {
            "status": "posted",
            "platform": "instagram",
            "publish_method": method,
            "graph_host": settings["graph_host"],
            "file_path": str(target),
            "hosted_video_url": hosted_video_url or None,
            "hosted_video_probe": hosted_video_probe,
            "container_id": container_id,
            "container_status": status_snapshot["status_code"],
            "share_to_feed": share_value,
            "media_id": media_id or None,
            "permalink": media_details.get("permalink"),
            "media_type": media_details.get("media_type"),
            "media_product_type": media_details.get("media_product_type"),
        }

    with sanitized_instagram_upload_file(target) as upload_target:
        if method == "quick_tunnel":
            with temporary_instagram_video_url(upload_target, settings=settings) as hosted_video:
                payload = build_instagram_reel_payload(
                    share_to_feed=share_value,
                    caption=instagram_caption,
                    thumb_offset_ms=thumb_offset_ms,
                    cover_url=cover_url,
                    audio_name=audio_name,
                    video_url=hosted_video["public_url"],
                )
                return publish_from_payload(
                    payload,
                    upload_target,
                    hosted_video_url=hosted_video["public_url"],
                    hosted_video_probe=hosted_video.get("probe"),
                )

        payload = build_instagram_reel_payload(
            share_to_feed=share_value,
            caption=instagram_caption,
            thumb_offset_ms=thumb_offset_ms,
            cover_url=cover_url,
            audio_name=audio_name,
            upload_type="resumable",
        )
        return publish_from_payload(payload, upload_target)


def tiktok_settings():
    raw_backend = (os.environ.get("TIKTOK_PUBLISH_BACKEND") or "").strip()
    buffer_api_key = (os.environ.get("BUFFER_API_KEY") or "").strip()
    publish_backend = normalize_tiktok_publish_backend(raw_backend)
    if not raw_backend and buffer_api_key:
        publish_backend = "buffer"
    access_token = (os.environ.get("TIKTOK_ACCESS_TOKEN") or "").strip()
    raw_description_text = os.environ.get("TIKTOK_DESCRIPTION_TEXT")
    if publish_backend != "buffer" and not access_token:
        raise RuntimeError(
            "Missing TIKTOK_ACCESS_TOKEN. Copy shorts/tiktok.env.example to shorts/tiktok.env and fill it in, "
            "or use shorts/tiktok_token.py to exchange an auth code for a user token."
        )
    return {
        "publish_backend": publish_backend,
        "access_token": access_token,
        "description_text": (
            DEFAULT_SHORTFORM_DESCRIPTION_TEXT
            if raw_description_text is None
            else raw_description_text.strip()
        ),
        "cta_text": (os.environ.get("TIKTOK_CTA_TEXT") or DEFAULT_INSTAGRAM_CTA_TEXT).strip(),
        "privacy_level": (os.environ.get("TIKTOK_PRIVACY_LEVEL") or "").strip(),
        "disable_comment": env_bool("TIKTOK_DISABLE_COMMENT", False),
        "disable_duet": env_bool("TIKTOK_DISABLE_DUET", False),
        "disable_stitch": env_bool("TIKTOK_DISABLE_STITCH", False),
        "is_aigc": env_bool("TIKTOK_AI_GENERATED", False),
        "poll_seconds": env_int("TIKTOK_POLL_SECONDS", DEFAULT_TIKTOK_POLL_SECONDS, minimum=1),
        "poll_timeout_seconds": env_int(
            "TIKTOK_POLL_TIMEOUT_SECONDS",
            DEFAULT_TIKTOK_POLL_TIMEOUT_SECONDS,
            minimum=5,
        ),
        "chunk_size_mb": env_int(
            "TIKTOK_UPLOAD_CHUNK_SIZE_MB",
            DEFAULT_TIKTOK_UPLOAD_CHUNK_SIZE_MB,
            minimum=5,
        ),
    }


def tiktok_hashtags():
    youtube_defaults = sanitize_hashtags(_split_env_list("YOUTUBE_HASHTAGS", DEFAULT_SHORTFORM_HASHTAGS))
    return sanitize_hashtags(_split_env_list("TIKTOK_HASHTAGS", youtube_defaults))


def build_tiktok_title(title="", settings=None):
    settings = settings or tiktok_settings()
    return build_shortform_caption(
        title,
        description_text=settings.get("description_text"),
        cta_text=settings.get("cta_text"),
        hashtags=tiktok_hashtags(),
    )


def tiktok_client_settings():
    client_key = (os.environ.get("TIKTOK_CLIENT_KEY") or "").strip()
    client_secret = (os.environ.get("TIKTOK_CLIENT_SECRET") or "").strip()
    if not client_key or not client_secret:
        raise RuntimeError(
            "Missing TIKTOK_CLIENT_KEY or TIKTOK_CLIENT_SECRET. Copy shorts/tiktok.env.example to "
            "shorts/tiktok.env and fill them in."
        )
    return {
        "client_key": client_key,
        "client_secret": client_secret,
    }


def tiktok_headers(access_token):
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
    }


def tiktok_token_form_headers():
    return {
        "Content-Type": "application/x-www-form-urlencoded",
        "Cache-Control": "no-cache",
    }


def buffer_settings():
    api_key = (os.environ.get("BUFFER_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError(
            "Missing BUFFER_API_KEY. Generate one in Buffer and add it to your shorts env file before using "
            "the Buffer publishing backends."
        )
    return {
        "api_key": api_key,
        "api_url": (os.environ.get("BUFFER_API_URL") or DEFAULT_BUFFER_API_URL).strip() or DEFAULT_BUFFER_API_URL,
        "instagram_channel_id": (os.environ.get("BUFFER_INSTAGRAM_CHANNEL_ID") or "").strip(),
        "instagram_channel_name": (os.environ.get("BUFFER_INSTAGRAM_CHANNEL_NAME") or "").strip(),
        "tiktok_channel_id": (os.environ.get("BUFFER_TIKTOK_CHANNEL_ID") or "").strip(),
        "tiktok_channel_name": (os.environ.get("BUFFER_TIKTOK_CHANNEL_NAME") or "").strip(),
        "cloudflared_bin": (os.environ.get("BUFFER_CLOUDFLARED_BIN") or DEFAULT_BUFFER_CLOUDFLARED_BIN).strip()
        or DEFAULT_BUFFER_CLOUDFLARED_BIN,
        "poll_seconds": env_int("BUFFER_POLL_SECONDS", DEFAULT_TIKTOK_POLL_SECONDS, minimum=1),
        "poll_timeout_seconds": env_int(
            "BUFFER_POLL_TIMEOUT_SECONDS",
            DEFAULT_TIKTOK_POLL_TIMEOUT_SECONDS,
            minimum=5,
        ),
        "quick_tunnel_timeout_seconds": env_int(
            "BUFFER_QUICK_TUNNEL_TIMEOUT_SECONDS",
            DEFAULT_BUFFER_QUICK_TUNNEL_TIMEOUT_SECONDS,
            minimum=5,
        ),
        "quick_tunnel_grace_seconds": env_int(
            "BUFFER_QUICK_TUNNEL_GRACE_SECONDS",
            DEFAULT_BUFFER_QUICK_TUNNEL_GRACE_SECONDS,
            minimum=0,
        ),
        "quick_tunnel_min_interval_seconds": env_int(
            "BUFFER_QUICK_TUNNEL_MIN_INTERVAL_SECONDS",
            DEFAULT_QUICK_TUNNEL_MIN_INTERVAL_SECONDS,
            minimum=0,
        ),
        "quick_tunnel_max_attempts": env_int(
            "BUFFER_QUICK_TUNNEL_MAX_ATTEMPTS",
            DEFAULT_QUICK_TUNNEL_MAX_ATTEMPTS,
            minimum=1,
        ),
        "quick_tunnel_retry_delay_seconds": env_int(
            "BUFFER_QUICK_TUNNEL_RETRY_DELAY_SECONDS",
            DEFAULT_QUICK_TUNNEL_RETRY_DELAY_SECONDS,
            minimum=1,
        ),
        "quick_tunnel_doh_url": (
            os.environ.get("BUFFER_QUICK_TUNNEL_DOH_URL") or DEFAULT_QUICK_TUNNEL_DOH_URL
        ).strip(),
        "quick_tunnel_settle_seconds": env_int(
            "BUFFER_QUICK_TUNNEL_SETTLE_SECONDS",
            DEFAULT_BUFFER_QUICK_TUNNEL_SETTLE_SECONDS,
            minimum=0,
        ),
        "tunnel_hold_seconds": env_int(
            "BUFFER_TUNNEL_HOLD_SECONDS",
            DEFAULT_BUFFER_TUNNEL_HOLD_SECONDS,
            minimum=0,
        ),
    }


def buffer_graphql(settings, query, *, variables=None, default_message="Buffer GraphQL request failed."):
    requests = require_requests()
    response = requests.post(
        settings["api_url"],
        headers={
            "Authorization": f"Bearer {settings['api_key']}",
            "Content-Type": "application/json",
        },
        json={
            "query": query,
            "variables": variables or {},
        },
        timeout=120,
    )
    if response.status_code >= 400:
        detail = response.text.strip()
        raise RuntimeError(f"HTTP {response.status_code} from Buffer API: {detail[:1000]}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Non-JSON response from Buffer API: {response.text[:1000]}") from exc
    errors = payload.get("errors") or []
    if errors:
        raise RuntimeError(f"{default_message} {json.dumps(errors, sort_keys=True)[:1500]}")
    return payload.get("data") or {}


def buffer_cache_key(settings):
    return hashlib.sha256((settings.get("api_key") or "").encode("utf-8")).hexdigest()[:24]


def load_buffer_channel_cache(settings=None):
    settings = settings or buffer_settings()
    cache = load_json_or_default(BUFFER_CHANNEL_CACHE_PATH, {})
    return dict((cache.get(buffer_cache_key(settings)) or {}).get("services") or {})


def save_buffer_channel_cache(channels, settings=None):
    settings = settings or buffer_settings()
    ensure_layout()
    cache = load_json_or_default(BUFFER_CHANNEL_CACHE_PATH, {})
    grouped = {}
    for channel in list(channels or []):
        service = (channel.get("service") or "").strip().lower()
        if not service:
            continue
        grouped.setdefault(service, []).append(
            {
                "id": channel.get("id"),
                "name": channel.get("name"),
                "displayName": channel.get("displayName"),
                "service": service,
                "isQueuePaused": channel.get("isQueuePaused"),
                "organization_id": channel.get("organization_id"),
                "organization_name": channel.get("organization_name"),
            }
        )
    cache[buffer_cache_key(settings)] = {
        "updated_at": now_utc_iso(),
        "services": grouped,
    }
    atomic_write_json(BUFFER_CHANNEL_CACHE_PATH, cache)


def load_buffer_schema_cache(settings=None):
    settings = settings or buffer_settings()
    cache = load_json_or_default(BUFFER_SCHEMA_CACHE_PATH, {})
    return dict(cache.get(buffer_cache_key(settings)) or {})


def save_buffer_instagram_reel_input_fields(reel_fields, settings=None, *, source="schema"):
    settings = settings or buffer_settings()
    ensure_layout()
    cache = load_json_or_default(BUFFER_SCHEMA_CACHE_PATH, {})
    existing = dict(cache.get(buffer_cache_key(settings)) or {})
    existing["updated_at"] = now_utc_iso()
    existing["instagram_reel_input_fields"] = dict(reel_fields or {})
    existing["instagram_reel_input_fields_source"] = source
    cache[buffer_cache_key(settings)] = existing
    atomic_write_json(BUFFER_SCHEMA_CACHE_PATH, cache)


def list_buffer_channels(settings=None):
    settings = settings or buffer_settings()
    orgs_data = buffer_graphql(
        settings,
        """
        query GetOrganizations {
          account {
            organizations {
              id
              name
            }
          }
        }
        """,
        default_message="Buffer organization lookup failed.",
    )
    organizations = list((orgs_data.get("account") or {}).get("organizations") or [])
    if not organizations:
        raise RuntimeError("No Buffer organizations were returned for this API key.")

    candidates = []
    for organization in organizations:
        channels_data = buffer_graphql(
            settings,
            """
            query GetChannels($organizationId: OrganizationId!) {
              channels(input: { organizationId: $organizationId }) {
                id
                name
                displayName
                service
                isQueuePaused
              }
            }
            """,
            variables={"organizationId": organization["id"]},
            default_message="Buffer channel lookup failed.",
        )
        for channel in list(channels_data.get("channels") or []):
            channel = dict(channel)
            service = (channel.get("service") or "").strip().lower()
            if not service:
                continue
            channel["service"] = service
            channel["organization_id"] = organization["id"]
            channel["organization_name"] = organization.get("name")
            candidates.append(channel)

    save_buffer_channel_cache(candidates, settings=settings)
    return candidates


def select_buffer_channel(service, candidates, settings):
    service = (service or "").strip().lower()
    configured_channel_id = (settings.get(f"{service}_channel_id") or "").strip()
    configured_channel_name = (settings.get(f"{service}_channel_name") or "").strip().lower()
    candidates = [dict(channel) for channel in list(candidates or []) if (channel.get("service") or "").strip().lower() == service]

    if configured_channel_id:
        for channel in candidates:
            if channel.get("id") == configured_channel_id:
                return channel
        raise RuntimeError(
            f"BUFFER_{service.upper()}_CHANNEL_ID={configured_channel_id!r} did not match any connected Buffer {service.title()} channels."
        )

    if configured_channel_name:
        named_candidates = [
            channel
            for channel in candidates
            if configured_channel_name
            in {
                (channel.get("name") or "").strip().lower(),
                (channel.get("displayName") or "").strip().lower(),
            }
        ]
        if len(named_candidates) == 1:
            return named_candidates[0]
        if len(named_candidates) > 1:
            raise RuntimeError(
                f"BUFFER_{service.upper()}_CHANNEL_NAME matched multiple Buffer {service.title()} channels. "
                f"Use BUFFER_{service.upper()}_CHANNEL_ID instead: {named_candidates}"
            )
        raise RuntimeError(
            f"BUFFER_{service.upper()}_CHANNEL_NAME={settings.get(f'{service}_channel_name')!r} did not match any connected Buffer {service.title()} channels."
        )

    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise RuntimeError(f"No {service.title()} channels are connected in Buffer for this API key.")
    raise RuntimeError(
        f"Multiple {service.title()} channels are connected in Buffer. Set BUFFER_{service.upper()}_CHANNEL_ID "
        f"or BUFFER_{service.upper()}_CHANNEL_NAME to pick one: {candidates}"
    )


def query_buffer_channel(service, settings=None):
    settings = settings or buffer_settings()
    service = (service or "").strip().lower()
    configured_channel_id = (settings.get(f"{service}_channel_id") or "").strip()
    configured_channel_name = (settings.get(f"{service}_channel_name") or "").strip()

    if configured_channel_id:
        return {
            "id": configured_channel_id,
            "name": configured_channel_name or None,
            "displayName": configured_channel_name or None,
            "service": service,
            "organization_id": None,
            "organization_name": None,
        }

    cached_channels = load_buffer_channel_cache(settings=settings).get(service) or []
    if cached_channels:
        return select_buffer_channel(service, cached_channels, settings)

    return select_buffer_channel(
        service,
        list_buffer_channels(settings=settings),
        settings,
    )


def query_buffer_tiktok_channel(settings=None):
    return query_buffer_channel("tiktok", settings=settings)


def query_buffer_instagram_channel(settings=None):
    return query_buffer_channel("instagram", settings=settings)


def query_buffer_post_error_fields(settings=None):
    settings = settings or buffer_settings()
    data = buffer_graphql(
        settings,
        """
        query GetPostPublishingErrorFields {
          errorType: __type(name: "PostPublishingError") {
            fields {
              name
            }
          }
        }
        """,
        default_message="Buffer schema lookup failed.",
    )
    fields = list(((data.get("errorType") or {}).get("fields")) or [])
    return [field.get("name") for field in fields if field.get("name")]


def query_buffer_create_post_input_fields(settings=None):
    settings = settings or buffer_settings()
    data = buffer_graphql(
        settings,
        """
        query GetCreatePostInputFields {
          inputType: __type(name: "CreatePostInput") {
            inputFields {
              name
              type {
                kind
                name
                ofType {
                  kind
                  name
                  ofType {
                    kind
                    name
                    ofType {
                      kind
                      name
                    }
                  }
                }
              }
            }
          }
        }
        """,
        default_message="Buffer create post schema lookup failed.",
    )
    fields = list(((data.get("inputType") or {}).get("inputFields")) or [])
    return [field for field in fields if field.get("name")]


def buffer_instagram_reel_input_fields(settings=None):
    return {}


def fetch_buffer_post(post_id, settings=None, *, error_fields=None):
    settings = settings or buffer_settings()
    buffer_error_fields = list(error_fields or [])
    error_block = ""
    if buffer_error_fields:
        error_fragment = "\n".join(f"      {name}" for name in buffer_error_fields)
        error_block = f"""
        error {{
{error_fragment}
        }}
"""
    query = f"""
    query GetPost($postId: PostId!) {{
      post(input: {{ id: $postId }}) {{
        id
        status
        text
        dueAt
        sentAt
        updatedAt
        shareMode
        schedulingType
        sharedNow
        assets {{
          source
        }}
{error_block}
        channel {{
          id
          name
          displayName
          service
        }}
      }}
    }}
    """
    data = buffer_graphql(
        settings,
        query,
        variables={"postId": post_id},
        default_message="Buffer post status fetch failed.",
    )
    return data.get("post") or {}


def format_buffer_post_snapshot(snapshot):
    data = snapshot or {}
    parts = []

    status = (data.get("status") or "").strip()
    if status:
        parts.append(f"status={status}")

    sent_at = (data.get("sentAt") or "").strip()
    if sent_at:
        parts.append(f"sentAt={sent_at}")

    error = data.get("error") or {}
    message = (error.get("message") or error.get("serviceErrorMessage") or "").strip()
    if message:
        parts.append(f"error={message}")

    return ", ".join(parts) or "no status details"


def build_buffer_post_result(platform, post_snapshot, channel, target, title, *, hosted_video_url=None):
    result = {
        "status": (post_snapshot.get("status") or "").strip().lower() or "unknown",
        "platform": platform,
        "provider": "buffer",
        "file_path": str(target),
        "title": title or None,
        "buffer_post_id": post_snapshot.get("id"),
        "buffer_share_mode": post_snapshot.get("shareMode"),
        "buffer_scheduling_type": post_snapshot.get("schedulingType"),
        "buffer_shared_now": post_snapshot.get("sharedNow"),
        "buffer_sent_at": post_snapshot.get("sentAt"),
        "buffer_channel_id": channel.get("id"),
        "buffer_channel_name": channel.get("displayName") or channel.get("name"),
        "buffer_organization": channel.get("organization_name"),
    }
    if hosted_video_url:
        result["hosted_video_url"] = hosted_video_url
    return result


def build_buffer_tiktok_result(post_snapshot, channel, target, tiktok_title, *, hosted_video_url=None):
    return build_buffer_post_result(
        "tiktok",
        post_snapshot,
        channel,
        target,
        tiktok_title,
        hosted_video_url=hosted_video_url,
    )


def build_buffer_instagram_result(post_snapshot, channel, target, instagram_caption, *, hosted_video_url=None):
    return build_buffer_post_result(
        "instagram",
        post_snapshot,
        channel,
        target,
        instagram_caption,
        hosted_video_url=hosted_video_url,
    )


def hold_buffer_tunnel_for_fetch(platform, hosted_video, hold_seconds):
    public_url = (hosted_video or {}).get("public_url") or ""
    print(f"Buffer {platform}: holding temporary video URL open for {hold_seconds}s: {public_url}")
    if hold_seconds > 0:
        time.sleep(hold_seconds)


def wait_for_buffer_post(post_id, settings=None, *, error_fields=None):
    settings = settings or buffer_settings()
    deadline = time.time() + settings["poll_timeout_seconds"]
    last_snapshot = None

    while time.time() <= deadline:
        snapshot = fetch_buffer_post(post_id, settings=settings, error_fields=error_fields)
        last_snapshot = snapshot
        status = (snapshot.get("status") or "").strip().lower()
        if status in {"sent", "error"}:
            return snapshot
        time.sleep(settings["poll_seconds"])

    raise RuntimeError(
        f"Timed out waiting for Buffer post {post_id}. Last snapshot: {format_buffer_post_snapshot(last_snapshot)}"
    )


@contextmanager
def temporary_buffer_video_url(file_path, settings=None):
    settings = settings or buffer_settings()
    with temporary_instagram_video_url(
        file_path,
        settings={
            "cloudflared_bin": settings["cloudflared_bin"],
            "quick_tunnel_timeout_seconds": settings["quick_tunnel_timeout_seconds"],
            "quick_tunnel_grace_seconds": settings["quick_tunnel_grace_seconds"],
            "quick_tunnel_min_interval_seconds": settings["quick_tunnel_min_interval_seconds"],
            "quick_tunnel_max_attempts": settings["quick_tunnel_max_attempts"],
            "quick_tunnel_retry_delay_seconds": settings["quick_tunnel_retry_delay_seconds"],
            "quick_tunnel_doh_url": settings["quick_tunnel_doh_url"],
            "quick_tunnel_settle_seconds": settings["quick_tunnel_settle_seconds"],
        },
    ) as hosted_video:
        yield hosted_video


def normalize_tiktok_token_bundle(data):
    return {
        "access_token": (data.get("access_token") or "").strip() or None,
        "refresh_token": (data.get("refresh_token") or "").strip() or None,
        "open_id": (data.get("open_id") or "").strip() or None,
        "scope": (data.get("scope") or "").strip() or None,
        "token_type": (data.get("token_type") or "").strip() or None,
        "expires_in": data.get("expires_in"),
        "refresh_expires_in": data.get("refresh_expires_in"),
    }


def tiktok_oauth_error_message(data, default_message):
    error_code = (data or {}).get("error")
    if not error_code:
        return ""
    parts = [default_message, f"error={error_code}"]
    description = (data.get("error_description") or "").strip()
    if description:
        parts.append(description)
    log_id = (data.get("log_id") or "").strip()
    if log_id:
        parts.append(f"log_id={log_id}")
    return " | ".join(parts)


def tiktok_oauth_token_request(form_payload, *, default_message):
    requests = require_requests()
    response = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        headers=tiktok_token_form_headers(),
        data=form_payload,
        timeout=120,
    )
    if response.status_code >= 400:
        detail = response.text.strip()
        raise RuntimeError(f"HTTP {response.status_code} from TikTok OAuth token endpoint: {detail[:500]}")
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Non-JSON response from TikTok OAuth token endpoint: {response.text[:500]}") from exc
    oauth_error = tiktok_oauth_error_message(data, default_message)
    if oauth_error:
        raise RuntimeError(oauth_error)
    return normalize_tiktok_token_bundle(data)


def exchange_tiktok_auth_code(code, redirect_uri, *, code_verifier=""):
    client = tiktok_client_settings()
    auth_code = (code or "").strip()
    redirect_value = (redirect_uri or "").strip()
    verifier = (code_verifier or "").strip()
    if not auth_code:
        raise RuntimeError("Missing TikTok authorization code.")
    if not redirect_value:
        raise RuntimeError("Missing TikTok redirect URI.")

    form_payload = {
        "client_key": client["client_key"],
        "client_secret": client["client_secret"],
        "code": auth_code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_value,
    }
    if verifier:
        form_payload["code_verifier"] = verifier
    return tiktok_oauth_token_request(
        form_payload,
        default_message="TikTok OAuth authorization_code exchange failed.",
    )


def refresh_tiktok_access_token(refresh_token=""):
    client = tiktok_client_settings()
    refresh_value = (refresh_token or os.environ.get("TIKTOK_REFRESH_TOKEN") or "").strip()
    if not refresh_value:
        raise RuntimeError("Missing TikTok refresh token.")

    form_payload = {
        "client_key": client["client_key"],
        "client_secret": client["client_secret"],
        "grant_type": "refresh_token",
        "refresh_token": refresh_value,
    }
    return tiktok_oauth_token_request(
        form_payload,
        default_message="TikTok OAuth refresh_token exchange failed.",
    )


def query_tiktok_creator_info(settings=None):
    settings = settings or tiktok_settings()
    data = http_json(
        "POST",
        "https://open.tiktokapis.com/v2/post/publish/creator_info/query/",
        headers=tiktok_headers(settings["access_token"]),
        payload={},
    )
    raise_tiktok_error(data, "TikTok creator_info query failed.")
    return data.get("data") or {}


def fetch_tiktok_publish_status(publish_id, settings=None):
    settings = settings or tiktok_settings()
    data = http_json(
        "POST",
        "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
        headers=tiktok_headers(settings["access_token"]),
        payload={"publish_id": publish_id},
    )
    raise_tiktok_error(data, "TikTok publish status fetch failed.")
    return data.get("data") or {}


def format_tiktok_publish_snapshot(snapshot):
    data = snapshot or {}
    parts = []

    status = (data.get("status") or "").strip()
    if status:
        parts.append(f"status={status}")

    publish_type = (data.get("publish_type") or "").strip()
    if publish_type:
        parts.append(f"publish_type={publish_type}")

    uploaded_bytes = data.get("uploaded_bytes")
    if uploaded_bytes is not None:
        parts.append(f"uploaded_bytes={uploaded_bytes}")

    downloaded_bytes = data.get("downloaded_bytes")
    if downloaded_bytes is not None:
        parts.append(f"downloaded_bytes={downloaded_bytes}")

    fail_reason = (data.get("fail_reason") or "").strip()
    if fail_reason:
        parts.append(f"fail_reason={fail_reason}")

    return ", ".join(parts) or "no status details"


def wait_for_tiktok_publish(publish_id, settings=None):
    settings = settings or tiktok_settings()
    deadline = time.time() + settings["poll_timeout_seconds"]
    last_snapshot = None

    while time.time() <= deadline:
        snapshot = fetch_tiktok_publish_status(publish_id, settings=settings)
        last_snapshot = snapshot
        status = (snapshot.get("status") or "").strip().upper()
        if status in TIKTOK_TERMINAL_PUBLISH_STATUSES:
            return snapshot
        time.sleep(settings["poll_seconds"])

    raise RuntimeError(
        f"Timed out waiting for TikTok publish {publish_id}. Last snapshot: "
        f"{format_tiktok_publish_snapshot(last_snapshot)}. TikTok does not guarantee a short processing window, "
        "so retry with --no-wait and inspect the publish_id later if the upload bytes already look complete."
    )


@contextmanager
def sanitized_instagram_upload_file(file_path):
    target = Path(file_path).expanduser().resolve()
    if not target.exists():
        raise FileNotFoundError(target)

    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise RuntimeError("ffmpeg is required to prepare Instagram uploads.")

    ffprobe_bin = shutil.which("ffprobe")
    if not ffprobe_bin:
        raise RuntimeError("ffprobe is required to prepare Instagram uploads.")

    probe_payload = probe_media_file(target, ffprobe_bin=ffprobe_bin)
    streams = probe_payload.get("streams") or []
    format_data = probe_payload.get("format") or {}
    has_audio_streams = any((stream.get("codec_type") or "").strip() == "audio" for stream in streams)
    try:
        duration_seconds = float(format_data.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration_seconds = 0.0
    if duration_seconds < 5.0:
        raise RuntimeError(f"Instagram Reels require a minimum of 5 seconds. This clip is {duration_seconds:.1f}s.")

    with tempfile.NamedTemporaryFile(prefix="instagram-upload-", suffix=".mp4", delete=False) as handle:
        sanitized_path = Path(handle.name)

    cmd = [
        ffmpeg_bin,
        "-y",
        "-i",
        str(target),
    ]
    if has_audio_streams:
        cmd.extend(
            [
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
            ]
        )
    else:
        cmd.extend(
            [
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
            ]
        )
    cmd.extend(
        [
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-r",
            "30",
            "-g",
            "60",
            "-keyint_min",
            "30",
            "-sc_threshold",
            "0",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-profile:v",
            "high",
            "-level:v",
            "4.1",
            "-movflags",
            "+faststart",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "48000",
            "-ac",
            "2",
        ]
    )
    if not has_audio_streams:
        cmd.append("-shortest")
    cmd.append(str(sanitized_path))

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        try:
            sanitized_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(f"Instagram upload sanitization failed: {result.stderr[-500:]}")

    try:
        sanitized_probe = probe_media_file(sanitized_path, ffprobe_bin=ffprobe_bin)
        sanitized_streams = sanitized_probe.get("streams") or []
        sanitized_video_stream = next(
            ((stream or {}) for stream in sanitized_streams if (stream.get("codec_type") or "").strip() == "video"),
            {},
        )
        sanitized_has_audio = any(
            (stream.get("codec_type") or "").strip() == "audio" for stream in sanitized_streams
        )
        sanitized_size_mb = sanitized_path.stat().st_size / (1024 * 1024)
        sanitized_fps = ffprobe_rate_to_fps(
            sanitized_video_stream.get("avg_frame_rate") or sanitized_video_stream.get("r_frame_rate") or ""
        )
        print(
            "instagram sanitized: "
            f"codec={sanitized_video_stream.get('codec_name') or 'unknown'} "
            f"fps={format_debug_fps(sanitized_fps)} "
            f"res={sanitized_video_stream.get('width') or 0}x{sanitized_video_stream.get('height') or 0} "
            f"audio={sanitized_has_audio} "
            f"size={sanitized_size_mb:.1f}MB"
        )
        yield sanitized_path
    finally:
        try:
            sanitized_path.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def sanitized_tiktok_upload_file(file_path):
    target = Path(file_path).expanduser().resolve()
    if not target.exists():
        raise FileNotFoundError(target)

    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise RuntimeError("ffmpeg is required to prepare TikTok uploads.")

    with tempfile.NamedTemporaryFile(prefix="tiktok-upload-", suffix=".mp4", delete=False) as handle:
        sanitized_path = Path(handle.name)

    cmd = [
        ffmpeg_bin,
        "-y",
        "-i",
        str(target),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(sanitized_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        try:
            sanitized_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(f"TikTok upload sanitization failed: {result.stderr[-500:]}")

    try:
        yield sanitized_path
    finally:
        try:
            sanitized_path.unlink(missing_ok=True)
        except OSError:
            pass


def tiktok_prepare_file_upload(file_path, settings=None):
    settings = settings or tiktok_settings()
    target = Path(file_path).expanduser().resolve()
    if not target.exists():
        raise FileNotFoundError(target)

    file_size = target.stat().st_size
    if file_size <= 0:
        raise RuntimeError(f"TikTok upload file is empty: {target}")

    min_chunk_size = 5 * 1024 * 1024
    max_chunk_size = 64 * 1024 * 1024
    configured_chunk_size = settings["chunk_size_mb"] * 1024 * 1024
    if file_size <= max_chunk_size:
        chunk_sizes = [file_size]
    else:
        chunk_size = min(max(configured_chunk_size, min_chunk_size), max_chunk_size)
        # TikTok expects any remainder to be folded into the final chunk instead of sent as a tiny tail chunk.
        total_chunk_count = file_size // chunk_size
        if total_chunk_count < 2:
            chunk_size = min(max(file_size // 2, min_chunk_size), max_chunk_size)
            total_chunk_count = file_size // chunk_size
        if total_chunk_count < 2:
            raise RuntimeError(
                f"Unable to build a valid TikTok chunk plan for {target} ({file_size} bytes)."
            )
        chunk_sizes = [chunk_size] * (total_chunk_count - 1)
        chunk_sizes.append(file_size - (chunk_size * (total_chunk_count - 1)))

    if len(chunk_sizes) > 1000:
        raise RuntimeError(
            f"TikTok upload would require {len(chunk_sizes)} chunks, which exceeds the API limit of 1000."
        )

    if len(chunk_sizes) > 1:
        if any(size < min_chunk_size or size > max_chunk_size for size in chunk_sizes[:-1]):
            raise RuntimeError(f"TikTok upload plan contains an invalid non-final chunk size: {chunk_sizes}")
        if not (min_chunk_size <= chunk_sizes[-1] <= 128 * 1024 * 1024):
            raise RuntimeError(f"TikTok upload plan contains an invalid final chunk size: {chunk_sizes[-1]}")

    return {
        "target": target,
        "file_size": file_size,
        "chunk_size": chunk_sizes[0],
        "chunk_sizes": chunk_sizes,
        "total_chunk_count": len(chunk_sizes),
    }


def tiktok_upload_file(upload_url, upload_plan):
    response_url = (upload_url or "").strip()
    if not response_url:
        raise RuntimeError("TikTok upload init returned no upload_url.")

    requests = require_requests()
    target = upload_plan["target"]
    file_size = upload_plan["file_size"]
    chunk_sizes = list(upload_plan.get("chunk_sizes") or [])
    if not chunk_sizes:
        raise RuntimeError("TikTok upload plan returned no chunks.")

    chunk_results = []
    with open(target, "rb") as handle:
        offset = 0
        for index, expected_size in enumerate(chunk_sizes, start=1):
            chunk = handle.read(expected_size)
            if len(chunk) != expected_size:
                raise RuntimeError(
                    f"TikTok upload chunk read was truncated at byte {offset}. "
                    f"Expected {expected_size} bytes but got {len(chunk)}."
                )
            end = offset + len(chunk) - 1
            request_range = f"bytes {offset}-{end}/{file_size}"
            upload_response = requests.put(
                response_url,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(chunk)),
                    "Content-Range": request_range,
                },
                data=chunk,
                timeout=600,
            )
            if upload_response.status_code >= 400:
                raise RuntimeError(
                    f"TikTok upload failed with HTTP {upload_response.status_code}: {upload_response.text[:500]}"
                )
            expected_status_code = 201 if index == len(chunk_sizes) else 206
            if upload_response.status_code != expected_status_code:
                raise RuntimeError(
                    "TikTok upload returned an unexpected response status. "
                    f"Chunk {index}/{len(chunk_sizes)} expected HTTP {expected_status_code} but got "
                    f"{upload_response.status_code}. Response headers: {dict(upload_response.headers)}"
                )
            chunk_results.append(
                {
                    "chunk_index": index,
                    "chunk_count": len(chunk_sizes),
                    "request_range": request_range,
                    "bytes_sent": len(chunk),
                    "response_status_code": upload_response.status_code,
                    "response_content_range": (upload_response.headers.get("Content-Range") or "").strip() or None,
                    "response_content_length": (upload_response.headers.get("Content-Length") or "").strip() or None,
                    "response_etag": (upload_response.headers.get("Etag") or "").strip() or None,
                    "response_log_id": (upload_response.headers.get("x-tt-logid") or "").strip() or None,
                }
            )
            offset = end + 1
        if offset != file_size:
            raise RuntimeError(f"TikTok upload ended at byte {offset}, expected {file_size}.")
    return {
        "chunk_count": len(chunk_sizes),
        "file_size": file_size,
        "chunks": chunk_results,
    }


def post_video_to_tiktok_via_buffer(
    file_path,
    title="",
    *,
    wait_for_finish=True,
    existing_platform_result=None,
    progress_callback=None,
):
    if not wait_for_finish:
        raise RuntimeError(
            "Buffer-backed TikTok publishing requires waiting so the temporary Cloudflare video URL stays online "
            "until Buffer finishes fetching and sending the video."
        )

    settings = tiktok_settings()
    buffer = buffer_settings()
    target = Path(file_path).expanduser().resolve()
    if not target.exists():
        raise FileNotFoundError(target)

    tiktok_title = build_tiktok_title(title, settings=settings)
    channel = query_buffer_tiktok_channel(settings=buffer)
    existing_result = dict(existing_platform_result or {})
    existing_buffer_post_id = (existing_result.get("buffer_post_id") or "").strip()

    if existing_buffer_post_id:
        return accept_buffer_tiktok_result(existing_result)

    with temporary_buffer_video_url(target, settings=buffer) as hosted_video:
        created = buffer_graphql(
            buffer,
            """
            mutation CreatePost($input: CreatePostInput!) {
              createPost(input: $input) {
                __typename
                ... on PostActionSuccess {
                  post {
                    id
                    status
                    text
                    shareMode
                    schedulingType
                    sharedNow
                    assets {
                      source
                    }
                  }
                }
                ... on MutationError {
                  message
                }
              }
            }
            """,
            variables={
                "input": {
                    "text": tiktok_title,
                    "channelId": channel["id"],
                    "schedulingType": "automatic",
                    "mode": "shareNow",
                    "source": "video_worker_tiktok_buffer",
                    "assets": {
                        "videos": [
                            {
                                "url": hosted_video["public_url"],
                            }
                        ]
                    },
                }
            },
            default_message="Buffer TikTok createPost failed.",
        ).get("createPost") or {}

        if created.get("__typename") != "PostActionSuccess":
            message = (created.get("message") or "Buffer TikTok createPost failed.").strip()
            raise RuntimeError(message)

        post_snapshot = created.get("post") or {}
        accepted_result = accept_buffer_tiktok_result(
            build_buffer_tiktok_result(
                post_snapshot,
                channel,
                target,
                tiktok_title,
                hosted_video_url=hosted_video["public_url"],
            )
        )
        if progress_callback and accepted_result:
            progress_callback(accepted_result)
        if not wait_for_finish:
            return accepted_result

        hold_seconds = int(buffer.get("tunnel_hold_seconds") or 0)
        hold_buffer_tunnel_for_fetch("TikTok", hosted_video, hold_seconds)

        return accepted_result


def upload_video_to_tiktok_draft(file_path, *, wait_for_finish=True):
    settings = tiktok_settings()
    if settings["publish_backend"] != "native":
        raise RuntimeError("TikTok draft upload is only supported with TIKTOK_PUBLISH_BACKEND=native.")
    original_target = str(Path(file_path).expanduser().resolve())

    with sanitized_tiktok_upload_file(file_path) as sanitized_path:
        upload_plan = tiktok_prepare_file_upload(sanitized_path, settings=settings)

        init_payload = {
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": upload_plan["file_size"],
                "chunk_size": upload_plan["chunk_size"],
                "total_chunk_count": upload_plan["total_chunk_count"],
            },
        }
        init_response = http_json(
            "POST",
            "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/",
            headers=tiktok_headers(settings["access_token"]),
            payload=init_payload,
            timeout=120,
        )
        raise_tiktok_error(init_response, "TikTok draft upload init failed.")

        response_data = init_response.get("data") or {}
        publish_id = (response_data.get("publish_id") or "").strip()
        if not publish_id:
            raise RuntimeError(f"TikTok draft upload init returned no publish_id: {init_response}")

        upload_summary = tiktok_upload_file(response_data.get("upload_url"), upload_plan)

        status_snapshot = fetch_tiktok_publish_status(publish_id, settings=settings)
        if wait_for_finish:
            status_snapshot = wait_for_tiktok_publish(publish_id, settings=settings)

        return {
            "status": (status_snapshot.get("status") or "").strip() or "PENDING",
            "platform": "tiktok",
            "delivery_mode": "draft_upload",
            "file_path": original_target,
            "publish_id": publish_id,
            "publish_type": status_snapshot.get("publish_type"),
            "public_post_ids": status_snapshot.get("publicaly_available_post_id")
            or status_snapshot.get("publicly_available_post_id")
            or [],
            "fail_reason": status_snapshot.get("fail_reason"),
            "uploaded_bytes": status_snapshot.get("uploaded_bytes"),
            "upload_summary": upload_summary,
        }


def post_video_to_tiktok(
    file_path,
    title="",
    *,
    privacy_level="",
    disable_comment=None,
    disable_duet=None,
    disable_stitch=None,
    cover_timestamp_ms=None,
    is_aigc=None,
    wait_for_finish=True,
    existing_platform_result=None,
    progress_callback=None,
):
    settings = tiktok_settings()
    if settings["publish_backend"] == "buffer":
        unsupported_options = []
        if privacy_level:
            unsupported_options.append("privacy_level")
        if disable_comment is not None:
            unsupported_options.append("disable_comment")
        if disable_duet is not None:
            unsupported_options.append("disable_duet")
        if disable_stitch is not None:
            unsupported_options.append("disable_stitch")
        if cover_timestamp_ms is not None:
            unsupported_options.append("cover_timestamp_ms")
        if is_aigc is not None:
            unsupported_options.append("is_aigc")
        if unsupported_options:
            raise RuntimeError(
                "The Buffer TikTok backend currently supports caption + video delivery only. "
                f"Unsupported options: {', '.join(unsupported_options)}"
            )
        return post_video_to_tiktok_via_buffer(
            file_path,
            title,
            wait_for_finish=wait_for_finish,
            existing_platform_result=existing_platform_result,
            progress_callback=progress_callback,
        )
    target = Path(file_path).expanduser().resolve()

    tiktok_title = build_tiktok_title(title, settings=settings)
    creator_info = query_tiktok_creator_info(settings=settings)
    privacy_options = list(creator_info.get("privacy_level_options") or [])
    requested_privacy = (privacy_level or settings["privacy_level"]).strip()
    if not requested_privacy and privacy_options:
        requested_privacy = privacy_options[0]
    if privacy_options and requested_privacy not in privacy_options:
        raise RuntimeError(
            f"TikTok privacy level {requested_privacy!r} is not allowed for this creator. "
            f"Available options: {privacy_options}"
        )

    comment_value = settings["disable_comment"] if disable_comment is None else bool(disable_comment)
    duet_value = settings["disable_duet"] if disable_duet is None else bool(disable_duet)
    stitch_value = settings["disable_stitch"] if disable_stitch is None else bool(disable_stitch)
    aigc_value = settings["is_aigc"] if is_aigc is None else bool(is_aigc)

    post_info = {
        "privacy_level": requested_privacy,
        "disable_comment": comment_value,
        "disable_duet": duet_value,
        "disable_stitch": stitch_value,
        "is_aigc": aigc_value,
    }
    if tiktok_title:
        post_info["title"] = tiktok_title
    if cover_timestamp_ms is not None:
        post_info["video_cover_timestamp_ms"] = int(cover_timestamp_ms)

    with sanitized_tiktok_upload_file(file_path) as sanitized_path:
        upload_plan = tiktok_prepare_file_upload(sanitized_path, settings=settings)

        init_payload = {
            "post_info": post_info,
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": upload_plan["file_size"],
                "chunk_size": upload_plan["chunk_size"],
                "total_chunk_count": upload_plan["total_chunk_count"],
            },
        }
        init_response = http_json(
            "POST",
            "https://open.tiktokapis.com/v2/post/publish/video/init/",
            headers=tiktok_headers(settings["access_token"]),
            payload=init_payload,
            timeout=120,
        )
        raise_tiktok_error(init_response, "TikTok publish init failed.")

        response_data = init_response.get("data") or {}
        publish_id = (response_data.get("publish_id") or "").strip()
        if not publish_id:
            raise RuntimeError(f"TikTok publish init returned no publish_id: {init_response}")

        upload_summary = tiktok_upload_file(response_data.get("upload_url"), upload_plan)

        status_snapshot = fetch_tiktok_publish_status(publish_id, settings=settings)
        if wait_for_finish:
            status_snapshot = wait_for_tiktok_publish(publish_id, settings=settings)

        return {
            "status": (status_snapshot.get("status") or "").strip() or "PENDING",
            "platform": "tiktok",
            "file_path": str(target),
            "title": tiktok_title or None,
            "publish_id": publish_id,
            "privacy_level": requested_privacy,
            "creator_username": creator_info.get("creator_username"),
            "creator_nickname": creator_info.get("creator_nickname"),
            "public_post_ids": status_snapshot.get("publicaly_available_post_id")
            or status_snapshot.get("publicly_available_post_id")
            or [],
            "fail_reason": status_snapshot.get("fail_reason"),
            "uploaded_bytes": status_snapshot.get("uploaded_bytes"),
            "upload_summary": upload_summary,
        }
