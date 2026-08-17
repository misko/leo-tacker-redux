"""Stable identifiers for recording geometries accepted by Starlink analysis."""

STARLINK_EDGE_SCAN_SCHEMA_V1 = "org.leo-flow.starlink-edge-scan/v1"
STARLINK_FOCUSED_MONITOR_SCHEMA_V1 = "org.leo-flow.starlink-focused-monitor/v1"
STARLINK_ANALYZABLE_SCAN_SCHEMAS_V1 = frozenset(
    {STARLINK_EDGE_SCAN_SCHEMA_V1, STARLINK_FOCUSED_MONITOR_SCHEMA_V1}
)
