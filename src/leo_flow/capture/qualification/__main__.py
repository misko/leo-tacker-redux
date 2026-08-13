"""Run explicit Pluto narrow/wide qualification and emit one JSON document."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from leo_flow.contracts.capture import GainMode, GainSetting
from leo_flow.contracts.core import RadioId, ReceiverChainId, canonical_json_bytes

from ..drivers import PlutoPairedRadio, PlutoRadioConfig
from .runner import CaptureQualificationRunner, QualificationProfile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--radio-id", required=True)
    parser.add_argument("--receiver-a", required=True)
    parser.add_argument("--receiver-b", required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--center-frequency-hz", type=float, required=True)
    parser.add_argument("--gain-mode", choices=("manual", "agc"), default="manual")
    parser.add_argument("--gain-db", type=float, default=50.0)
    parser.add_argument("--narrow-duration-s", type=float, default=120.0)
    parser.add_argument("--wide-duration-s", type=float, default=10.0)
    parser.add_argument("--block-samples", type=int, default=262_144)
    parser.add_argument("--alignment-expected", type=Path)
    parser.add_argument("--interruption-bytes", type=int, default=8 * 1024 * 1024)
    args = parser.parse_args(argv)
    try:
        receivers = (
            ReceiverChainId(args.receiver_a),
            ReceiverChainId(args.receiver_b),
        )
        gain = (
            GainSetting(GainMode.MANUAL, args.gain_db)
            if args.gain_mode == "manual"
            else GainSetting(GainMode.AGC)
        )
        config = PlutoRadioConfig(
            uri=args.uri,
            expected_serial=args.serial,
            radio_id=RadioId(args.radio_id),
            receiver_chain_ids=receivers,
            block_samples=args.block_samples,
        )
        expected = None
        if args.alignment_expected is not None:
            if args.alignment_expected.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("alignment input is limited to 8 MiB")
            expected = args.alignment_expected.read_bytes()
        runner = CaptureQualificationRunner(
            PlutoPairedRadio(config), receivers, args.output_directory
        )
        result = runner.run(
            (
                QualificationProfile(
                    "narrow",
                    args.center_frequency_hz,
                    2_500_000.0,
                    2_500_000.0,
                    args.narrow_duration_s,
                    gain,
                ),
                QualificationProfile(
                    "wide",
                    args.center_frequency_hz,
                    10_000_000.0,
                    9_000_000.0,
                    args.wide_duration_s,
                    gain,
                ),
            ),
            alignment_expected=expected,
            interruption_bytes=args.interruption_bytes,
        )
        payload: object = result
        exit_code = (
            0 if result.all_profiles_succeeded and result.interruption.success else 1
        )
    except Exception as error:  # noqa: BLE001 - CLI emits one machine-readable result
        payload = {
            "schema": "org.leo-flow.capture-qualification/v1",
            "error": {"type": type(error).__name__, "message": str(error)},
        }
        exit_code = 2
    sys.stdout.buffer.write(canonical_json_bytes(payload) + b"\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
