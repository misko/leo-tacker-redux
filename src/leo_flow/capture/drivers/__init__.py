"""Optional hardware adapters for capture."""

from .pluto import PlutoPairedRadio, PlutoRadioConfig
from .spf_v3 import (
    SpfV3MetadataReader,
    normalize_spf_v3_metadata,
    spf_iio_session_factory,
)
from .v5_preflight import (
    ExpectedV5Radio,
    ExpectedV5Runtime,
    ObservedV5Radio,
    ObservedV5Runtime,
    StandardLibiioTransport,
    V5Attestation,
    attest_v5,
    create_attested_v5_radio,
)

__all__ = [
    "ExpectedV5Radio",
    "ExpectedV5Runtime",
    "ObservedV5Radio",
    "ObservedV5Runtime",
    "PlutoPairedRadio",
    "PlutoRadioConfig",
    "SpfV3MetadataReader",
    "StandardLibiioTransport",
    "V5Attestation",
    "attest_v5",
    "create_attested_v5_radio",
    "normalize_spf_v3_metadata",
    "spf_iio_session_factory",
]
