from __future__ import annotations

import re
from importlib.resources import files

import pytest

_SEPARATELY_OWNED_ASSETS = {"dashboard.js", "recording-evidence.js"}


@pytest.mark.parametrize(
    "asset",
    sorted(
        candidate.name
        for candidate in (files("leo_flow.dashboard") / "static").iterdir()
        if candidate.name.endswith(".js")
        and candidate.name not in _SEPARATELY_OWNED_ASSETS
    ),
)
def test_every_remaining_shipped_javascript_asset_uses_semantic_api_routes(
    asset: str,
) -> None:
    javascript = (files("leo_flow.dashboard") / "static" / asset).read_text(
        encoding="utf-8"
    )

    assert re.search(r"/api/v\d+(?:/|\b)", javascript) is None
