"""Command-line host for a fully supplied deployment plugin."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Sequence
from enum import IntEnum
from pathlib import Path
from typing import TextIO

from .bootstrap import BootstrapError, DeploymentPlugin, assemble_service
from .config import ConfigurationError, load_service_config
from .lifecycle import JsonLineDiagnosticSink


class ExitCode(IntEnum):
    OK = 0
    USAGE_OR_CONFIG = 2
    BOOTSTRAP = 3
    RUNTIME = 4


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    parser = argparse.ArgumentParser(
        prog="leo-flow-service",
        description="Run one strictly scoped leo-flow service deployment plugin.",
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--plugin",
        required=True,
        metavar="MODULE:ATTRIBUTE",
        help="explicit DeploymentPlugin object (imports must be side-effect free)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run at most one unit")
    mode.add_argument(
        "--forever", action="store_true", help="run until SIGINT or SIGTERM (default)"
    )
    try:
        args = parser.parse_args(argv)
        config = load_service_config(args.config)
    except ConfigurationError:
        _error(stderr, "configuration_error")
        return ExitCode.USAGE_OR_CONFIG

    try:
        plugin = _load_plugin(args.plugin)
        service = assemble_service(
            config, plugin, diagnostics=JsonLineDiagnosticSink(stdout)
        )
    except BootstrapError:
        _error(stderr, "bootstrap_error")
        return ExitCode.BOOTSTRAP

    try:
        if args.once:
            try:
                service.run_once()
            finally:
                service.shutdown()
        else:
            service.run_forever()
    except (Exception, KeyboardInterrupt):  # noqa: BLE001 - process boundary
        _error(stderr, "runtime_error")
        return ExitCode.RUNTIME
    return ExitCode.OK


def _load_plugin(specification: str) -> DeploymentPlugin:
    module_name, separator, attribute = specification.partition(":")
    if not separator or not module_name or not attribute or ":" in attribute:
        raise BootstrapError("plugin must use MODULE:ATTRIBUTE syntax")
    if any(
        part.casefold() in {"latest", "default"}
        for part in (*module_name.split("."), attribute)
    ):
        raise BootstrapError("ambient plugin aliases are forbidden")
    try:
        value = getattr(importlib.import_module(module_name), attribute)
    except Exception as error:
        raise BootstrapError("deployment plugin cannot be loaded") from error
    if not isinstance(value, DeploymentPlugin):
        raise BootstrapError("plugin attribute is not a DeploymentPlugin")
    return value


def _error(stream: TextIO, event: str) -> None:
    stream.write(json.dumps({"event": event}, separators=(",", ":")) + "\n")
    stream.flush()


if __name__ == "__main__":  # pragma: no cover - exercised through main
    raise SystemExit(main())
