"""Narrow command-line wrapper for the exact dashboard deployment."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, TextIO

from leo_flow.services.cli import main as run_service

DEFAULT_CONFIG = Path("/etc/leo-flow/dashboard.json")
PLUGIN_SPEC = "leo_flow.deployments.dashboard_v1:PLUGIN"


class ServiceRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str] | None = None,
        *,
        stdout: TextIO = sys.stdout,
        stderr: TextIO = sys.stderr,
    ) -> int: ...


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leo-dashboard",
        description="Run the exact read-only LEO Flow dashboard deployment.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run at most one request")
    mode.add_argument(
        "--forever",
        action="store_true",
        help="run until SIGINT or SIGTERM (default)",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    service_runner: ServiceRunner = run_service,
) -> int:
    arguments = _parser().parse_args(argv)
    service_arguments = [
        "--config",
        str(arguments.config),
        "--plugin",
        PLUGIN_SPEC,
        "--once" if arguments.once else "--forever",
    ]
    return service_runner(service_arguments, stdout=stdout, stderr=stderr)


if __name__ == "__main__":  # pragma: no cover - console script owns this path
    raise SystemExit(main())
