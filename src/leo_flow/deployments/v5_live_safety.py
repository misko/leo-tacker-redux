"""Read-only safety observations shared by supervised V5 live harnesses."""

from __future__ import annotations

import importlib

IO_TIMEOUT_MS = 5_000


class V5LiveSafetyError(RuntimeError):
    """The selected live radio is not the exact expected muted V5 device."""


def verify_tx2_muted(uri: str, expected_serial: str) -> dict[str, object]:
    """Read TX2 identity/gain/DDS state without writing a radio attribute."""

    iio = importlib.import_module("iio")
    context = iio.Context(uri)
    try:
        set_timeout = getattr(context, "set_timeout", None)
        if not callable(set_timeout):
            raise V5LiveSafetyError("TX mute check cannot install a finite timeout")
        set_timeout(IO_TIMEOUT_MS)
        serial = str(context.attrs["hw_serial"])
        if serial != expected_serial:
            raise V5LiveSafetyError("TX mute check reached a different radio serial")
        phy = context.find_device("ad9361-phy")
        dds = context.find_device("cf-ad9361-dds-core-lpc")
        if phy is None or dds is None:
            raise V5LiveSafetyError("TX mute check cannot find PHY or DDS")
        tx2_gain = None
        for channel in phy.channels:
            if channel.output and channel.id == "voltage1":
                tx2_gain = float(channel.attrs["hardwaregain"].value.split()[0])
                break
        if tx2_gain is None or tx2_gain > -80.0:
            raise V5LiveSafetyError("TX2 hardware gain is not at the muted floor")
        scales: dict[str, float] = {}
        for channel in dds.channels:
            if channel.output and channel.id in {
                "altvoltage4",
                "altvoltage5",
                "altvoltage6",
                "altvoltage7",
            }:
                scales[channel.id] = float(channel.attrs["scale"].value)
        if set(scales) != {
            "altvoltage4",
            "altvoltage5",
            "altvoltage6",
            "altvoltage7",
        } or any(value != 0.0 for value in scales.values()):
            raise V5LiveSafetyError("TX2 DDS scales are not all zero")
        return {
            "radio_serial": serial,
            "tx2_hardware_gain_db": tx2_gain,
            "tx2_dds_scales": scales,
            "read_only_check": True,
        }
    finally:
        destroy = getattr(context, "destroy", None)
        if callable(destroy):
            destroy()
