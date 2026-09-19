"""Transport-neutral mass-distribution protocol and benchmark decision rules."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from pyfog.schemas import Schema
from pyfog.transfer import TransferBlock, TransferManifest

DistributionStrategy = Literal["unicast", "relay", "p2p", "multicast"]
MAX_MULTICAST_REPAIR_RATIO = 0.15
MAX_RECEIVER_ID_LENGTH = 128
MAX_REPAIR_INDICES = 4096
MAX_REPAIR_SECRET_BYTES = 128
MIN_REPAIR_SECRET_BYTES = 16


class DistributionError(RuntimeError):
    """A distribution session or decision is not safe to run."""


class DistributionTopology(Schema):
    """Explicit network gates required before enabling a data-plane strategy."""

    igmp_snooping: bool = Field(default=False, strict=True)
    multicast_acl_isolated: bool = Field(default=False, strict=True)
    unicast_repair_endpoint: bool = Field(default=True, strict=True)
    fixed_tracker: bool = Field(default=False, strict=True)
    relay_nodes: int = Field(default=0, ge=0, le=128, strict=True)
    external_p2p_disabled: bool = Field(default=True, strict=True)
    multicast_enabled: bool = Field(default=False, strict=True)


class DistributionMeasurement(Schema):
    """One reproducible benchmark result for a fixed artifact and receiver cohort."""

    strategy: DistributionStrategy
    artifact_size_bytes: int = Field(gt=0, le=2**63 - 1, strict=True)
    receiver_count: int = Field(ge=1, le=10_000, strict=True)
    failed_receivers: int = Field(default=0, ge=0, strict=True)
    duration_seconds: float = Field(gt=0, le=7 * 24 * 3600)
    seeder_bytes: int = Field(ge=0, le=2**63 - 1, strict=True)
    lan_bytes: int = Field(ge=0, le=2**63 - 1, strict=True)
    repair_bytes: int = Field(default=0, ge=0, le=2**63 - 1, strict=True)
    cpu_seconds: float = Field(default=0.0, ge=0, le=7 * 24 * 3600)
    memory_peak_bytes: int = Field(default=0, ge=0, le=2**63 - 1, strict=True)
    staging_bytes_per_second: float = Field(default=0.0, ge=0, le=2**63 - 1)
    packet_loss_ratio: float = Field(default=0.0, ge=0, le=1)
    vlan_isolation_verified: bool = Field(default=False, strict=True)
    slow_receiver_isolated: bool = Field(default=True, strict=True)
    completed: bool = Field(default=True, strict=True)
    isolated_failures: bool = Field(default=True, strict=True)
    resumed_receiver: bool = Field(default=False, strict=True)
    late_join: bool = Field(default=False, strict=True)
    seeder_failover: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def validate_scenario(self) -> DistributionMeasurement:
        if self.failed_receivers > self.receiver_count:
            msg = "No pueden fallar más receptores que los participantes."
            raise ValueError(msg)
        if self.completed and self.failed_receivers and not self.isolated_failures:
            msg = (
                "Un benchmark completado no puede cancelar todos los receptores "
                "por un fallo aislado."
            )
            raise ValueError(msg)
        if self.repair_bytes > self.lan_bytes:
            msg = "Las reparaciones no pueden superar el tráfico total medido."
            raise ValueError(msg)
        if self.strategy == "multicast" and self.completed and not self.isolated_failures:
            msg = "Una medición multicast exitosa debe aislar los fallos de receptores."
            raise ValueError(msg)
        return self

    @property
    def repair_ratio(self) -> float:
        return self.repair_bytes / max(1, self.lan_bytes)

    @property
    def seeder_ratio(self) -> float:
        return self.seeder_bytes / self.artifact_size_bytes


class DistributionDecision(Schema):
    """Auditable strategy selection; no strategy is enabled without evidence."""

    strategy: DistributionStrategy
    enabled: bool = Field(strict=True)
    requires_more_evidence: bool = Field(strict=True)
    fallback: DistributionStrategy
    reason: str = Field(min_length=1, max_length=500)
    benchmark_count: int = Field(ge=0, strict=True)


def choose_distribution(
    measurements: Sequence[DistributionMeasurement],
    topology: DistributionTopology,
) -> DistributionDecision:
    """Choose a data plane only after its failure and traffic gates pass.

    The initial recommendation remains private P2P with an HTTPS web seed, matching ADR-0002.
    Multicast is deliberately opt-in and needs both network prerequisites and a successful repair
    benchmark; otherwise the per-receiver task falls back to unicast/relay without affecting peers.
    """

    if (
        topology.igmp_snooping
        and topology.multicast_acl_isolated
        and topology.unicast_repair_endpoint
    ):
        candidates = [
            item
            for item in measurements
            if item.strategy == "multicast"
            and item.completed
            and item.isolated_failures
            and item.repair_ratio <= MAX_MULTICAST_REPAIR_RATIO
            and (item.late_join or item.receiver_count == 1)
            and (item.seeder_failover or item.failed_receivers == 0)
        ]
        if candidates:
            return DistributionDecision(
                strategy="multicast",
                enabled=topology.multicast_enabled,
                requires_more_evidence=not topology.multicast_enabled,
                fallback="p2p",
                reason="IGMP/ACL, reparación unicast y tolerancia de fallos fueron medidos.",
                benchmark_count=len(candidates),
            )
    p2p = [
        item
        for item in measurements
        if item.strategy == "p2p"
        and item.completed
        and item.isolated_failures
        and topology.fixed_tracker
        and topology.external_p2p_disabled
        and (item.resumed_receiver or item.receiver_count == 1)
    ]
    if p2p:
        return DistributionDecision(
            strategy="p2p",
            enabled=True,
            requires_more_evidence=False,
            fallback="relay" if topology.relay_nodes else "unicast",
            reason="P2P privado con tracker fijo y HTTP seed satisface el experimento de ADR-0002.",
            benchmark_count=len(p2p),
        )
    relay = [
        item
        for item in measurements
        if item.strategy == "relay" and item.completed and item.isolated_failures
    ]
    if relay and topology.relay_nodes:
        return DistributionDecision(
            strategy="relay",
            enabled=True,
            requires_more_evidence=False,
            fallback="unicast",
            reason=(
                "Los relays cacheados reducen la carga del seeder y mantienen reparación unicast."
            ),
            benchmark_count=len(relay),
        )
    return DistributionDecision(
        strategy="unicast",
        enabled=True,
        requires_more_evidence=not bool(measurements),
        fallback="unicast",
        reason=(
            "Se conserva HTTPS unicast hasta contar con evidencia reproducible del plano masivo."
        ),
        benchmark_count=len(measurements),
    )


@dataclass(frozen=True)
class MulticastPiece:
    """One immutable piece sent once to the multicast group."""

    session_id: UUID
    block: TransferBlock
    payload: bytes


class RepairRequest(Schema):
    """Receiver-local NACK; it never becomes a broadcast ACK barrier."""

    session_id: UUID
    receiver_id: str = Field(
        min_length=1, max_length=MAX_RECEIVER_ID_LENGTH, pattern=r"^[A-Za-z0-9._:-]+$"
    )
    missing_indices: list[int] = Field(min_length=1, max_length=MAX_REPAIR_INDICES)
    auth_tag: str = Field(default="", min_length=0, max_length=128, pattern=r"^[0-9a-f]*$")

    @model_validator(mode="after")
    def validate_indices(self) -> RepairRequest:
        if any(index < 0 for index in self.missing_indices) or len(
            set(self.missing_indices)
        ) != len(self.missing_indices):
            msg = "La solicitud de reparación contiene índices inválidos."
            raise ValueError(msg)
        return self


@dataclass
class _ReceiverState:
    received: set[int] = field(default_factory=set)
    repair_requests: int = 0
    duplicate_pieces: int = 0
    bytes_received: int = 0


class ReliableMulticastSession:
    """Receiver state machine for multicast pieces plus unicast repair."""

    def __init__(
        self,
        manifest: TransferManifest,
        *,
        session_id: UUID | None = None,
        repair_secret: bytes | None = None,
    ) -> None:
        self.manifest = manifest
        self.session_id = session_id or uuid4()
        self._repair_secret = repair_secret or secrets.token_bytes(32)
        if (
            not isinstance(self._repair_secret, bytes)
            or not MIN_REPAIR_SECRET_BYTES <= len(self._repair_secret) <= MAX_REPAIR_SECRET_BYTES
        ):
            msg = "El secreto de reparación no tiene un tamaño seguro."
            raise DistributionError(msg)
        self._states: dict[str, _ReceiverState] = {}

    def add_receiver(self, receiver_id: str) -> None:
        if not receiver_id or len(receiver_id) > MAX_RECEIVER_ID_LENGTH:
            msg = "La identidad del receptor no es válida."
            raise DistributionError(msg)
        if receiver_id in self._states:
            msg = "El receptor ya pertenece a la sesión."
            raise DistributionError(msg)
        self._states[receiver_id] = _ReceiverState()

    def receive(self, receiver_id: str, block: TransferBlock, payload: bytes) -> bool:
        state = self._state(receiver_id)
        self._validate_piece(block, payload)
        if block.index in state.received:
            state.duplicate_pieces += 1
            return True
        state.received.add(block.index)
        state.bytes_received += len(payload)
        return False

    def missing(self, receiver_id: str) -> tuple[int, ...]:
        state = self._state(receiver_id)
        return tuple(
            block.index for block in self.manifest.blocks if block.index not in state.received
        )

    def repair_request(self, receiver_id: str, *, max_indices: int = 1024) -> RepairRequest | None:
        missing = self.missing(receiver_id)
        if not missing:
            return None
        if max_indices <= 0 or max_indices > MAX_REPAIR_INDICES:
            msg = "El límite de reparación no es válido."
            raise DistributionError(msg)
        indices = list(missing[:max_indices])
        return RepairRequest(
            session_id=self.session_id,
            receiver_id=receiver_id,
            missing_indices=indices,
            auth_tag=self._repair_tag(receiver_id, indices),
        )

    def validate_repair_request(self, request: RepairRequest) -> tuple[int, ...]:
        """Authenticate a receiver-local NACK before serving unicast repair bytes."""

        if request.session_id != self.session_id:
            msg = "La solicitud de reparación pertenece a otra sesión."
            raise DistributionError(msg)
        state = self._state(request.receiver_id)
        expected = self._repair_tag(request.receiver_id, request.missing_indices)
        if not request.auth_tag or not hmac.compare_digest(request.auth_tag, expected):
            msg = "La solicitud de reparación no pasó autenticación."
            raise DistributionError(msg)
        missing = set(self.missing(request.receiver_id))
        if any(index not in missing for index in request.missing_indices):
            msg = "La solicitud de reparación contiene piezas ya recibidas."
            raise DistributionError(msg)
        state.repair_requests += 1
        return tuple(request.missing_indices)

    def complete(self, receiver_id: str) -> bool:
        return not self.missing(receiver_id)

    def state(self, receiver_id: str) -> dict[str, int | bool]:
        state = self._state(receiver_id)
        return {
            "received_blocks": len(state.received),
            "missing_blocks": len(self.missing(receiver_id)),
            "repair_requests": state.repair_requests,
            "duplicate_pieces": state.duplicate_pieces,
            "bytes_received": state.bytes_received,
            "complete": self.complete(receiver_id),
        }

    def metrics(self) -> dict[str, int]:
        """Return aggregate session metrics without receiver identifiers or image content."""

        return {
            "receivers": len(self._states),
            "completed": sum(self.complete(receiver_id) for receiver_id in self._states),
            "repair_requests": sum(state.repair_requests for state in self._states.values()),
            "duplicate_pieces": sum(state.duplicate_pieces for state in self._states.values()),
            "bytes_received": sum(state.bytes_received for state in self._states.values()),
        }

    def _state(self, receiver_id: str) -> _ReceiverState:
        try:
            return self._states[receiver_id]
        except KeyError:
            msg = "El receptor no pertenece a la sesión."
            raise DistributionError(msg) from None

    def _validate_piece(self, block: TransferBlock, payload: bytes) -> None:
        if block.index >= len(self.manifest.blocks) or self.manifest.blocks[block.index] != block:
            msg = "La pieza no pertenece al manifiesto de la sesión."
            raise DistributionError(msg)
        if len(payload) != block.size or hashlib.sha256(payload).hexdigest() != block.sha256:
            msg = "La pieza multicast no pasó la verificación SHA-256."
            raise DistributionError(msg)

    def _repair_tag(self, receiver_id: str, indices: Sequence[int]) -> str:
        encoded = ",".join(str(index) for index in indices).encode("ascii")
        return hmac.new(
            self._repair_secret,
            self.session_id.bytes + receiver_id.encode("utf-8") + b"\0" + encoded,
            hashlib.sha256,
        ).hexdigest()


def iter_multicast_pieces(
    path: Path, manifest: TransferManifest, *, session_id: UUID
) -> Iterator[MulticastPiece]:
    """Read one artifact sequentially and yield verified pieces without buffering it all."""

    try:
        with path.open("rb") as source:
            for block in manifest.blocks:
                source.seek(block.offset)
                payload = source.read(block.size)
                if (
                    len(payload) != block.size
                    or hashlib.sha256(payload).hexdigest() != block.sha256
                ):
                    msg = "El artefacto no coincide con el manifiesto de piezas."
                    raise DistributionError(msg)
                yield MulticastPiece(session_id, block, payload)
    except OSError as error:
        msg = f"No se pudo leer el artefacto multicast: {error}"
        raise DistributionError(msg) from None


def validate_multicast_topology(topology: DistributionTopology) -> None:
    """Fail closed before a multicast socket is opened."""

    if not topology.igmp_snooping:
        msg = "La red no declara IGMP snooping administrado."
        raise DistributionError(msg)
    if not topology.multicast_acl_isolated:
        msg = "La red multicast no está aislada por ACL."
        raise DistributionError(msg)
    if not topology.unicast_repair_endpoint:
        msg = "Multicast requiere un endpoint de reparación unicast."
        raise DistributionError(msg)


def multicast_can_start(topology: DistributionTopology) -> bool:
    """Return whether the experimental multicast feature flag and network gates are on."""

    if not topology.multicast_enabled:
        return False
    validate_multicast_topology(topology)
    return True


def benchmark_summary(measurements: Sequence[DistributionMeasurement]) -> dict[str, object]:
    """Produce a stable, secret-free report suitable for the ADR review."""

    if not measurements:
        msg = "El benchmark no contiene mediciones."
        raise DistributionError(msg)
    by_strategy: dict[str, dict[str, object]] = {}
    for strategy in ("unicast", "relay", "p2p", "multicast"):
        values = [item for item in measurements if item.strategy == strategy]
        if not values:
            continue
        by_strategy[strategy] = {
            "runs": len(values),
            "completed": sum(item.completed for item in values),
            "max_receivers": max(item.receiver_count for item in values),
            "max_seeder_ratio": max(item.seeder_ratio for item in values),
            "max_repair_ratio": max(item.repair_ratio for item in values),
            "max_cpu_seconds": max(item.cpu_seconds for item in values),
            "max_memory_peak_bytes": max(item.memory_peak_bytes for item in values),
            "min_staging_bytes_per_second": min(item.staging_bytes_per_second for item in values),
            "max_packet_loss_ratio": max(item.packet_loss_ratio for item in values),
            "vlan_isolation_verified": all(item.vlan_isolation_verified for item in values),
            "slow_receiver_isolated": all(item.slow_receiver_isolated for item in values),
            "isolated_failures": all(item.isolated_failures for item in values),
        }
    return {"strategies": by_strategy, "runs": len(measurements)}
