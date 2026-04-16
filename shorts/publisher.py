#!/usr/bin/env python3

import json
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fcntl

SHORTS_DIR = Path(__file__).resolve().parent
REPO_DIR = SHORTS_DIR.parent
CLIPS_DIR = SHORTS_DIR / "clips"
STATE_DIR = SHORTS_DIR / "state"
QUEUE_PATH = STATE_DIR / "queue.json"
LOCK_PATH = STATE_DIR / "queue.lock"
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


def mark_posted(queue_id, youtube_video_id, youtube_url, file_cleanup=None):
    file_cleanup = file_cleanup or {}

    def updater(item):
        item["status"] = "posted"
        item["posted_at"] = now_utc_iso()
        item["youtube_video_id"] = youtube_video_id
        item["youtube_url"] = youtube_url
        item["last_error"] = None
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
    client_secret = Path(
        (os.environ.get("YOUTUBE_CLIENT_SECRET_FILE") or "").strip() or DEFAULT_CLIENT_SECRET_PATH
    )
    token_path = Path((os.environ.get("YOUTUBE_TOKEN_FILE") or "").strip() or DEFAULT_TOKEN_PATH)
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


def post_next_queued_short(dry_run=False, interactive_auth=False):
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
            "cleanup": cleanup_summary,
        }

    try:
        result = upload_short_to_youtube(entry, interactive=interactive_auth)
    except Exception as exc:
        mark_failed(entry["queue_id"], str(exc))
        cleanup_summary = run_storage_maintenance()
        return {
            "status": "error",
            "queue_id": entry["queue_id"],
            "message": str(exc),
            "cleanup": cleanup_summary,
        }

    file_cleanup = remove_posted_clip_file(entry["file_path"])
    mark_posted(entry["queue_id"], result["video_id"], result["youtube_url"], file_cleanup=file_cleanup)
    cleanup_summary = run_storage_maintenance()
    return {
        "status": "posted",
        "queue_id": entry["queue_id"],
        "video_id": result["video_id"],
        "youtube_url": result["youtube_url"],
        "title": result["title"],
        "file_path": entry["file_path"],
        "local_file_deleted": file_cleanup["deleted"],
        "local_file_delete_error": file_cleanup["error"],
        "cleanup": cleanup_summary,
    }
