"""Strict deployment bootstrap for independently capable service processes.

This module intentionally contains no production adapters. A deployment plugin
supplies exact, side-effect-free factories; service I/O begins only when the
assembled :class:`ServiceLoop` runs its preflight.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Protocol, TypeAlias, cast

from .config import (
    AnalysisServiceConfig,
    CaptureServiceConfig,
    DashboardServiceConfig,
    SecretRef,
    ServiceConfig,
)
from .lifecycle import DiagnosticSink, ServiceLoop


class BootstrapError(RuntimeError):
    """A deployment manifest cannot safely assemble the requested process."""


class Process(str, Enum):
    CAPTURE = "capture"
    ANALYSIS = "analysis"
    DASHBOARD = "dashboard"


class Capability(str, Enum):
    PLAN_SOURCE = "plan_source"
    RADIO = "radio"
    CAPTURE_PREFLIGHT = "capture_preflight"
    RECORDING_WRITER = "recording_writer"
    SPOOL = "spool"
    RECORDING_PUBLISHER = "recording_publisher"
    JOB_REPOSITORY = "job_repository"
    RECORDING_READER = "recording_reader"
    FEATURE_PUBLISHER = "feature_publisher"
    MODEL_PUBLISHER = "model_publisher"
    QUERY_PROJECTION = "query_projection"
    DASHBOARD_SERVER = "dashboard_server"


_ALLOWED_CAPABILITIES: Mapping[Process, frozenset[Capability]] = MappingProxyType(
    {
        Process.CAPTURE: frozenset(
            {
                Capability.PLAN_SOURCE,
                Capability.RADIO,
                Capability.CAPTURE_PREFLIGHT,
                Capability.RECORDING_WRITER,
                Capability.SPOOL,
                Capability.RECORDING_PUBLISHER,
            }
        ),
        Process.ANALYSIS: frozenset(
            {
                Capability.JOB_REPOSITORY,
                Capability.RECORDING_READER,
                Capability.FEATURE_PUBLISHER,
                Capability.MODEL_PUBLISHER,
            }
        ),
        Process.DASHBOARD: frozenset(
            {Capability.QUERY_PROJECTION, Capability.DASHBOARD_SERVER}
        ),
    }
)


class SecretProvider(Protocol):
    """Resolve one named secret from an explicitly injected provider."""

    def resolve(self, name: str) -> str: ...


@dataclass(frozen=True)
class AdapterBuildContext:
    process: Process
    capability: Capability
    reference: str
    secrets: Mapping[SecretRef, str]


AdapterFactory: TypeAlias = Callable[[AdapterBuildContext], object]
AdapterSet: TypeAlias = Mapping[Capability, object]
ProcessBuilder: TypeAlias = Callable[
    [ServiceConfig, AdapterSet, DiagnosticSink], ServiceLoop
]


class AdapterManifest:
    """Immutable exact-reference registries partitioned by process capability."""

    def __init__(
        self,
        registries: Mapping[Process, Mapping[Capability, Mapping[str, AdapterFactory]]],
    ) -> None:
        frozen: dict[Process, Mapping[Capability, Mapping[str, AdapterFactory]]] = {}
        for process, capabilities in registries.items():
            allowed = _ALLOWED_CAPABILITIES[process]
            unexpected = set(capabilities) - allowed
            if unexpected:
                names = sorted(item.value for item in unexpected)
                raise BootstrapError(
                    f"{process.value} manifest contains forbidden capabilities: {names}"
                )
            frozen[process] = MappingProxyType(
                {
                    capability: MappingProxyType(dict(factories))
                    for capability, factories in capabilities.items()
                }
            )
        self._registries = MappingProxyType(frozen)

    def factory(
        self, process: Process, capability: Capability, reference: str
    ) -> AdapterFactory:
        if capability not in _ALLOWED_CAPABILITIES[process]:
            raise BootstrapError(
                f"{process.value} cannot resolve {capability.value} capability"
            )
        _validate_exact_reference(reference)
        try:
            return self._registries[process][capability][reference]
        except KeyError as error:
            raise BootstrapError(
                f"no exact {capability.value} adapter for {process.value}"
            ) from error


@dataclass(frozen=True)
class DeploymentPlugin:
    """Complete deployment-owned registries and process builders."""

    manifest: AdapterManifest
    secret_providers: Mapping[str, SecretProvider]
    builders: Mapping[Process, ProcessBuilder]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "secret_providers", MappingProxyType(dict(self.secret_providers))
        )
        object.__setattr__(self, "builders", MappingProxyType(dict(self.builders)))


_CONFIG_REFS: Mapping[Process, tuple[tuple[Capability, str], ...]] = MappingProxyType(
    {
        Process.CAPTURE: (
            (Capability.PLAN_SOURCE, "plan_source_ref"),
            (Capability.RADIO, "radio_ref"),
            (Capability.CAPTURE_PREFLIGHT, "preflight_ref"),
            (Capability.RECORDING_WRITER, "recording_writer_ref"),
            (Capability.SPOOL, "spool_ref"),
            (Capability.RECORDING_PUBLISHER, "recording_publisher_ref"),
        ),
        Process.ANALYSIS: (
            (Capability.JOB_REPOSITORY, "job_repository_ref"),
            (Capability.RECORDING_READER, "recording_reader_ref"),
            (Capability.FEATURE_PUBLISHER, "feature_publisher_ref"),
            (Capability.MODEL_PUBLISHER, "model_publisher_ref"),
        ),
        Process.DASHBOARD: (
            (Capability.QUERY_PROJECTION, "query_projection_ref"),
            (Capability.DASHBOARD_SERVER, "server_ref"),
        ),
    }
)


def assemble_service(
    config: ServiceConfig,
    plugin: DeploymentPlugin,
    *,
    diagnostics: DiagnosticSink,
) -> ServiceLoop:
    """Validate the complete assembly, then construct its adapters and loop.

    Missing builders, factories, providers, or aliases are rejected before any
    adapter factory is invoked. Secret values are available only in immutable
    factory context and are never copied into configuration or diagnostics.
    """

    process = Process(config.process)
    expected_type = {
        Process.CAPTURE: CaptureServiceConfig,
        Process.ANALYSIS: AnalysisServiceConfig,
        Process.DASHBOARD: DashboardServiceConfig,
    }[process]
    if not isinstance(config, expected_type):
        raise BootstrapError("configuration process and type disagree")
    try:
        builder = plugin.builders[process]
    except KeyError as error:
        raise BootstrapError(f"no {process.value} process builder") from error

    selected: list[tuple[Capability, str, AdapterFactory]] = []
    for capability, attribute in _CONFIG_REFS[process]:
        reference = cast(str, getattr(config, attribute))
        selected.append(
            (
                capability,
                reference,
                plugin.manifest.factory(process, capability, reference),
            )
        )

    providers: list[tuple[SecretRef, SecretProvider]] = []
    for secret_ref in config.runtime.secret_refs:
        try:
            provider = plugin.secret_providers[secret_ref.provider]
        except KeyError as error:
            raise BootstrapError("configured secret provider is unavailable") from error
        providers.append((secret_ref, provider))

    resolved: dict[SecretRef, str] = {}
    for secret_ref, provider in providers:
        try:
            value = provider.resolve(secret_ref.name)
        except Exception as error:
            raise BootstrapError("secret resolution failed") from error
        if not isinstance(value, str) or not value:
            raise BootstrapError("secret provider returned an empty value")
        resolved[secret_ref] = value
    secrets = MappingProxyType(resolved)

    adapters: dict[Capability, object] = {}
    for capability, reference, factory in selected:
        try:
            adapters[capability] = factory(
                AdapterBuildContext(process, capability, reference, secrets)
            )
        except Exception as error:
            raise BootstrapError(
                f"{capability.value} adapter construction failed"
            ) from error

    try:
        return builder(config, MappingProxyType(adapters), diagnostics)
    except Exception as error:
        raise BootstrapError(f"{process.value} process assembly failed") from error


def _validate_exact_reference(reference: str) -> None:
    parts = re.split(r"[._:/-]", reference.casefold())
    if "latest" in parts or "default" in parts:
        raise BootstrapError("ambient adapter aliases are forbidden")
