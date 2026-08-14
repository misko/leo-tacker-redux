"""Operator CLI for authoritative hardware metadata snapshots."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .operator import (
    HardwareBundleIdentity,
    HardwareOperatorError,
    create_bundle,
    load_operator_config,
    publish_bundle,
    validate_bundle,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="leo-flow-hardware")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="create one canonical bundle")
    create.add_argument("--config", required=True, type=Path)
    create.add_argument("--output", required=True, type=Path)

    validate = commands.add_parser("validate", help="validate exact bundle bytes")
    validate.add_argument("--bundle", required=True, type=Path)

    publish = commands.add_parser("publish", help="publish one exact bundle")
    publish.add_argument("--config", required=True, type=Path)
    publish.add_argument("--bundle", required=True, type=Path)
    publish.add_argument(
        "--dry-run",
        action="store_true",
        help="validate without resolving credentials or writing CAS/PostgreSQL",
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            identity = create_bundle(load_operator_config(args.config), args.output)
            event = "hardware_bundle_created"
        elif args.command == "validate":
            _, identity = validate_bundle(args.bundle)
            event = "hardware_bundle_validated"
        else:
            identity = publish_bundle(
                load_operator_config(args.config),
                args.bundle,
                dry_run=args.dry_run,
            )
            event = (
                "hardware_publication_validated"
                if args.dry_run
                else "hardware_snapshot_published"
            )
    except HardwareOperatorError:
        sys.stderr.write('{"event":"hardware_operator_failed"}\n')
        return 3

    sys.stdout.write(_result(event, identity) + "\n")
    return 0


def _result(event: str, identity: HardwareBundleIdentity) -> str:
    return json.dumps(
        {
            "byte_count": identity.byte_count,
            "digest_algorithm": identity.ref.digest.algorithm.value,
            "digest_value": identity.ref.digest.value,
            "event": event,
            "snapshot_id": str(identity.ref.snapshot_id),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


if __name__ == "__main__":
    raise SystemExit(main())
