"""Capture-owned operational errors."""


class CaptureError(RuntimeError):
    """Base class for acquisition failures visible to the scheduler."""


class RadioConfigurationError(CaptureError):
    pass


class TuningError(CaptureError):
    pass


class RadioDisconnectedError(CaptureError):
    pass


class RefillError(CaptureError):
    pass


class ReceiverSkewError(CaptureError):
    pass


class SampleCountError(CaptureError):
    pass


class WriterIdentityError(CaptureError):
    pass
