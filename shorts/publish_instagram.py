#!/usr/bin/env python3

import argparse
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

from shorts.publisher import post_reel_to_instagram


def main():
    parser = argparse.ArgumentParser(description="Post a local mp4 to Instagram as a Reel.")
    parser.add_argument("--file", required=True, help="Path to the local mp4 file to publish.")
    parser.add_argument("--caption", default="", help="Caption to publish with the Reel.")
    parser.add_argument("--thumb-offset-ms", type=int, help="Frame timestamp in milliseconds for the cover.")
    parser.add_argument("--cover-url", default="", help="Optional public JPEG URL for the Reel cover image.")
    parser.add_argument("--audio-name", default="", help="Optional original-audio display name.")
    parser.add_argument(
        "--reels-only",
        action="store_true",
        help="Publish to the Reels tab only instead of sharing to the main feed too.",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Skip waiting for Meta processing to finish before calling media_publish. Not supported with quick tunnels.",
    )
    parser.add_argument(
        "--resumable-upload",
        action="store_true",
        help="Use the older Meta resumable upload flow instead of the default quick-tunnel video_url flow.",
    )
    args = parser.parse_args()

    result = post_reel_to_instagram(
        args.file,
        args.caption,
        share_to_feed=not args.reels_only,
        thumb_offset_ms=args.thumb_offset_ms,
        cover_url=args.cover_url,
        audio_name=args.audio_name,
        wait_for_finish=not args.no_wait,
        publish_method="resumable" if args.resumable_upload else None,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
