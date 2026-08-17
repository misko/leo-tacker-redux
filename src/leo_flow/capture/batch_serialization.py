"""Compatibility names for the public capture-batch contract codec.

Capture durability and operators consume the same strict public document.  No
private SQLite or filesystem representation crosses this module.
"""

from leo_flow.contracts.capture_batch_codec import (
    CaptureBatchDocumentError as CaptureBatchCodecError,
)
from leo_flow.contracts.capture_batch_codec import (
    decode_capture_batch_definition as decode_batch_definition,
)
from leo_flow.contracts.capture_batch_codec import (
    decode_capture_batch_snapshot as decode_batch_snapshot,
)
from leo_flow.contracts.capture_batch_codec import (
    encode_capture_batch_definition as encode_batch_definition,
)
from leo_flow.contracts.capture_batch_codec import (
    encode_capture_batch_snapshot as encode_batch_snapshot,
)

__all__ = (
    "CaptureBatchCodecError",
    "decode_batch_definition",
    "decode_batch_snapshot",
    "encode_batch_definition",
    "encode_batch_snapshot",
)
