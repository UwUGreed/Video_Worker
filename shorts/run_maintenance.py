#!/usr/bin/env python3

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

from shorts.publisher import run_storage_maintenance


def main():
    print(json.dumps(run_storage_maintenance(), indent=2))


if __name__ == "__main__":
    main()
