#!/usr/bin/env python3
"""Verify the V5 runtime before executing its capture-worker command."""

from __future__ import annotations

import os
import subprocess
import sys

VERIFIER = "/opt/leo-v5/bin/verify-runtime"


def main(arguments: list[str]) -> int:
    result = subprocess.run([VERIFIER], check=False)
    if result.returncode != 0 or not arguments:
        return result.returncode
    os.execvp(arguments[0], arguments)
    raise AssertionError("execvp returned")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
