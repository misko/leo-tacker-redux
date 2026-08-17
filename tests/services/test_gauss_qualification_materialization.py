from __future__ import annotations

import json
from hashlib import sha256
from io import StringIO
from pathlib import Path

import pytest

from leo_flow.deployments.gauss_qualification_materialization import (
    main,
    validate_qualification_materialization,
)

REPOSITORY = Path("/home/mouse9911/gits/leo-tracker-redux")
ARTIFACT_ROOT = (
    REPOSITORY / "deploy/gauss-campaign-r20-r21-postreboot-v1/qualification-v5"
)
MANIFEST = ARTIFACT_ROOT / "qualification.materialization.json"


def test_historical_r20_r21_materialization_is_immutable_and_obsolete() -> None:
    definition = ARTIFACT_ROOT / "qualification.definition.json"

    assert sha256(MANIFEST.read_bytes()).hexdigest() == (
        "56292d23475d05399ff5026bc1b27f1b2b8ec2b4c07183d74d6b2f9e143ba164"
    )
    assert sha256(definition.read_bytes()).hexdigest() == (
        "9a1091b07917a74e815cbba3a64283450d63f06a89513afeb135e7c9ffeb72fc"
    )
    with pytest.raises(ValueError, match="materialized station differs"):
        validate_qualification_materialization(MANIFEST)


def test_r20_r21_validator_fails_closed_on_either_tx_policy_mutation(
    tmp_path: Path,
) -> None:
    value = json.loads(MANIFEST.read_text(encoding="utf-8"))
    value["preflight_policy"]["maximum_tx1_hardware_gain_db"] = -79.0
    mutated = tmp_path / "qualification.materialization.json"
    mutated.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    stdout = StringIO()
    stderr = StringIO()

    code = main(
        ["validate", "--manifest", str(mutated)],
        stdout=stdout,
        stderr=stderr,
    )

    assert code == 2
    assert stdout.getvalue() == ""
    assert json.loads(stderr.getvalue()) == {
        "event": "qualification_materialization_error"
    }


def test_r20_r21_validator_rejects_obsolete_one_refill_materialization() -> None:
    stdout = StringIO()
    stderr = StringIO()
    code = main(
        ["validate", "--manifest", str(MANIFEST)],
        stdout=stdout,
        stderr=stderr,
    )

    assert code == 2
    assert stdout.getvalue() == ""
    assert json.loads(stderr.getvalue()) == {
        "event": "qualification_materialization_error"
    }


def test_r20_r21_profile_binds_exact_lnb_receiver_mapping() -> None:
    value = json.loads(MANIFEST.read_text(encoding="utf-8"))
    source = Path(value["source_stations"]["b"]["path"])
    document = json.loads(source.read_text(encoding="utf-8"))

    assert document["radio"]["receiver_chain_ids"] == ["rx_lnb_c", "rx_lnb_d"]
    assert document["radio"]["expected_serial"] == (
        "10400056f695001322002d0010ad1719f2"
    )
    assert document["radio"]["require_both_tx_muted"] is True
