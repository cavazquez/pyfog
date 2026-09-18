from pathlib import Path
from uuid import uuid4

import pytest

from pyfog.distribution import (
    DistributionError,
    DistributionMeasurement,
    DistributionTopology,
    ReliableMulticastSession,
    benchmark_summary,
    choose_distribution,
    iter_multicast_pieces,
    multicast_can_start,
    validate_multicast_topology,
)
from pyfog.transfer import build_transfer_manifest


def measurement(strategy: str, **overrides: object) -> DistributionMeasurement:
    value: dict[str, object] = {
        "strategy": strategy,
        "artifact_size_bytes": 1000,
        "receiver_count": 4,
        "duration_seconds": 2.0,
        "seeder_bytes": 1500,
        "lan_bytes": 5000,
        "repair_bytes": 100,
        "resumed_receiver": True,
        "late_join": True,
        "seeder_failover": True,
    }
    value.update(overrides)
    return DistributionMeasurement.model_validate(value)


def test_multicast_receiver_requests_only_missing_blocks(tmp_path: Path):
    source = tmp_path / "piece.bin"
    source.write_bytes(b"a" * 100 + b"b" * 100)
    manifest = build_transfer_manifest(source, block_size=100)
    session = ReliableMulticastSession(manifest, session_id=uuid4())
    session.add_receiver("host-a")
    pieces = list(iter_multicast_pieces(source, manifest, session_id=session.session_id))
    assert session.receive("host-a", pieces[0].block, pieces[0].payload) is False
    request = session.repair_request("host-a")
    assert request is not None
    assert request.missing_indices == [1]
    assert session.validate_repair_request(request) == (1,)
    assert session.state("host-a")["repair_requests"] == 1
    session.receive("host-a", pieces[1].block, pieces[1].payload)
    assert session.complete("host-a")


def test_multicast_rejects_corrupt_piece_and_missing_network_gates(tmp_path: Path):
    manifest_source = b"a" * 100
    manifest_path = tmp_path / "piece.bin"
    try:
        manifest_path.write_bytes(manifest_source)
        manifest = build_transfer_manifest(manifest_path, block_size=100)
    finally:
        manifest_path.unlink(missing_ok=True)
    session = ReliableMulticastSession(manifest)
    session.add_receiver("host-a")
    with pytest.raises(DistributionError, match="SHA"):
        session.receive("host-a", manifest.blocks[0], b"x" * 100)
    with pytest.raises(DistributionError, match="IGMP"):
        validate_multicast_topology(DistributionTopology())
    assert multicast_can_start(DistributionTopology()) is False

    enabled = DistributionTopology(
        multicast_enabled=True,
        igmp_snooping=True,
        multicast_acl_isolated=True,
    )
    assert multicast_can_start(enabled) is True


def test_decision_prefers_measured_private_p2p_and_multicast_only_with_gates():
    p2p = measurement("p2p")
    p2p_decision = choose_distribution([p2p], DistributionTopology(fixed_tracker=True))
    assert p2p_decision.strategy == "p2p"
    multicast = measurement("multicast", seeder_bytes=1000)
    topology = DistributionTopology(
        igmp_snooping=True,
        multicast_acl_isolated=True,
        fixed_tracker=True,
    )
    assert choose_distribution([multicast], topology).strategy == "multicast"
    summary = benchmark_summary([p2p, multicast])
    assert summary["runs"] == 2
