#!/usr/bin/env python3

import argparse
import json
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

from shorts.publisher import (
    TIKTOK_TERMINAL_PUBLISH_STATUSES,
    fetch_tiktok_publish_status,
    format_tiktok_publish_snapshot,
)

TERMINAL_STATUSES = set(TIKTOK_TERMINAL_PUBLISH_STATUSES)


def fetch_snapshot(publish_id):
    snapshot = fetch_tiktok_publish_status(publish_id)
    status = (snapshot.get("status") or "UNKNOWN").strip().upper() or "UNKNOWN"
    return status, snapshot


def main():
    parser = argparse.ArgumentParser(description="Check the current status of a TikTok Content Posting publish_id.")
    parser.add_argument("publish_id", help="TikTok publish_id returned by the direct-post or draft-upload API.")
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Poll until TikTok reaches a terminal status or the timeout expires.",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=5,
        help="Polling interval when --watch is enabled. Defaults to 5 seconds.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=300,
        help="Maximum time to watch before exiting. Defaults to 300 seconds.",
    )
    args = parser.parse_args()

    if not args.watch:
        _, snapshot = fetch_snapshot(args.publish_id)
        print(json.dumps(snapshot, indent=2))
        return

    deadline = time.time() + max(args.timeout_seconds, 1)
    interval = max(args.interval_seconds, 1)
    last_status = None
    last_snapshot = None

    while time.time() <= deadline:
        status, snapshot = fetch_snapshot(args.publish_id)
        last_snapshot = snapshot
        if status != last_status:
            print(json.dumps(snapshot, indent=2))
            print("")
            last_status = status
        if status in TERMINAL_STATUSES:
            return
        time.sleep(interval)

    if last_snapshot is not None:
        print(json.dumps(last_snapshot, indent=2))
    raise SystemExit(
        "Timed out after "
        f"{args.timeout_seconds} seconds waiting for TikTok publish_id {args.publish_id}. "
        f"Last snapshot: {format_tiktok_publish_snapshot(last_snapshot)}"
    )


if __name__ == "__main__":
    main()
