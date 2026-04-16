#!/usr/bin/env python3

import argparse
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

from shorts.publisher import authorize_youtube, post_next_queued_short, queue_summary


def main():
    parser = argparse.ArgumentParser(description="Post the next queued short to YouTube.")
    parser.add_argument("--dry-run", action="store_true", help="Preview the next queued upload without posting it.")
    parser.add_argument("--authorize", action="store_true", help="Run the one-time YouTube OAuth flow.")
    parser.add_argument("--status", action="store_true", help="Print queue counts and exit.")
    args = parser.parse_args()

    if args.authorize:
        print(json.dumps(authorize_youtube(), indent=2))
        return

    if args.status:
        print(json.dumps(queue_summary(), indent=2))
        return

    result = post_next_queued_short(dry_run=args.dry_run)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
