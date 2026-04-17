#!/usr/bin/env python3

import argparse
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

from shorts.publisher import post_video_to_tiktok, upload_video_to_tiktok_draft


def main():
    parser = argparse.ArgumentParser(description="Post a local mp4 to TikTok or upload it as a draft.")
    parser.add_argument("--file", required=True, help="Path to the local mp4 file to publish.")
    parser.add_argument("--title", default="", help="TikTok caption/title for the direct post.")
    parser.add_argument("--privacy-level", default="", help="Override the privacy level for a direct post.")
    parser.add_argument("--disable-comment", action="store_true", help="Disable comments on the post.")
    parser.add_argument("--disable-duet", action="store_true", help="Disable duets on the post.")
    parser.add_argument("--disable-stitch", action="store_true", help="Disable stitches on the post.")
    parser.add_argument("--cover-timestamp-ms", type=int, help="Cover frame timestamp in milliseconds.")
    parser.add_argument("--aigc", action="store_true", help="Mark the post as AI-generated content.")
    parser.add_argument(
        "--buffer",
        action="store_true",
        help="Publish through Buffer instead of the native TikTok Content Posting API.",
    )
    parser.add_argument(
        "--draft",
        action="store_true",
        help="Upload the video to the creator's TikTok inbox for manual editing/posting instead of direct posting.",
    )
    parser.add_argument("--no-wait", action="store_true", help="Return after upload instead of polling status.")
    args = parser.parse_args()

    if args.buffer:
        os.environ["TIKTOK_PUBLISH_BACKEND"] = "buffer"
    if args.buffer and args.draft:
        parser.error("Buffer publishing does not support TikTok inbox drafts. Remove --draft or switch back to native.")
    if args.buffer and args.no_wait:
        parser.error("Buffer publishing requires waiting so the temporary public video URL stays online.")

    if args.draft:
        unsupported_flags = []
        if args.title:
            unsupported_flags.append("--title")
        if args.privacy_level:
            unsupported_flags.append("--privacy-level")
        if args.disable_comment:
            unsupported_flags.append("--disable-comment")
        if args.disable_duet:
            unsupported_flags.append("--disable-duet")
        if args.disable_stitch:
            unsupported_flags.append("--disable-stitch")
        if args.cover_timestamp_ms is not None:
            unsupported_flags.append("--cover-timestamp-ms")
        if args.aigc:
            unsupported_flags.append("--aigc")
        if unsupported_flags:
            parser.error(
                "TikTok draft upload does not accept direct-post metadata up front. "
                f"Remove these flags and finish the caption/privacy/editing inside TikTok: {', '.join(unsupported_flags)}"
            )
        result = upload_video_to_tiktok_draft(
            args.file,
            wait_for_finish=not args.no_wait,
        )
    else:
        result = post_video_to_tiktok(
            args.file,
            args.title,
            privacy_level=args.privacy_level,
            disable_comment=args.disable_comment if args.disable_comment else None,
            disable_duet=args.disable_duet if args.disable_duet else None,
            disable_stitch=args.disable_stitch if args.disable_stitch else None,
            cover_timestamp_ms=args.cover_timestamp_ms,
            is_aigc=args.aigc if args.aigc else None,
            wait_for_finish=not args.no_wait,
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
