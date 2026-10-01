from collections.abc import Sequence
from pathlib import Path

import polars as pl
import pytest

from netsleuth.detector import detect_anomalies
from netsleuth.eval import (
    AmplifierFailureSpec,
    Case,
    FaultSpec,
    IngressNoiseSpec,
    simulate_case,
)
from netsleuth.sandbox.topology import Amplifier, Modem, Node, Topology, generate_topology
from netsleuth.storage import DuckDBStorage


@pytest.fixture(scope="module")
def topo() -> Topology:
    return generate_topology("dev", seed=0)


def anomalies(tmp_path: Path, faults: Sequence[FaultSpec], ticks: int) -> pl.DataFrame:
    case = Case(case_id="c", ticks=ticks, faults=list(faults))
    sim = simulate_case(case, tmp_path / "data", tmp_path / "gt")
    with DuckDBStorage(tmp_path / "data").session("c", sim.engine.time_of(ticks - 1)) as s:
        return detect_anomalies(s)


def test_healthy_day_raises_nothing_on_any_signal(tmp_path: Path) -> None:
    assert anomalies(tmp_path, [], 288).height == 0


def test_ingress_raises_snr_and_t3_anomalies_at_the_evening_onset(
    topo: Topology, tmp_path: Path
) -> None:
    node = topo.of_type(Node)[0]
    sg = topo.parent(node.device_id)
    assert sg is not None
    found = anomalies(tmp_path, [IngressNoiseSpec(node_id=node.device_id, at_tick=0)], 288)

    snr = found.filter(pl.col("signal") == "sg_snr")
    t3 = found.filter(pl.col("signal") == "t3_rate")
    assert snr["scope_device_id"].to_list() == [sg.device_id]
    assert snr["tick"].to_list() == [17 * 12]
    assert set(t3["scope_device_id"]) <= {n.device_id for n in topo.children(sg.device_id)}
    assert t3.height >= 1
    assert int(t3["tick"].min()) <= 17 * 12 + 3  # type: ignore[arg-type]
    assert found.filter(pl.col("signal") == "share_offline").height == 0


def test_partial_amplifier_failure_raises_an_rf_drop_on_its_node(
    topo: Topology, tmp_path: Path
) -> None:
    amp = max(
        topo.of_type(Amplifier),
        key=lambda a: sum(isinstance(d, Modem) for d in topo.subtree(a.device_id)),
    )
    node = next(a for a in topo.ancestors(amp.device_id) if isinstance(a, Node))
    found = anomalies(
        tmp_path, [AmplifierFailureSpec(amp_id=amp.device_id, at_tick=20, partial=True)], 48
    )

    assert found["signal"].to_list() == ["rf_level_drop"]
    assert found["scope_device_id"].to_list() == [node.device_id]
    assert found["tick"][0] in (21, 24)  # the first RF poll at or after the failure
    assert found["offline_modems"].null_count() == 1


def test_mild_ingress_is_caught_by_the_hourly_t3_share(topo: Topology, tmp_path: Path) -> None:
    # Bring the low channels to about 29 dB, just under the T3 knee: only about 2% of modems log
    # a T3 in any one tick, but across an hour well over 5% have.
    node = topo.of_type(Node)[0]
    sg = topo.parent(node.device_id)
    assert sg is not None
    healthy = simulate_case(Case(case_id="h", ticks=1), tmp_path / "hd", tmp_path / "hg")
    with DuckDBStorage(tmp_path / "hd").session("h", healthy.engine.time_of(0)) as s:
        low = s.query(
            "SELECT min(us_snr_db) AS low FROM sg_channels "
            "WHERE sg_id = ? AND channel IN ('us1', 'us2')",
            [sg.device_id],
        )["low"][0]
    mild = IngressNoiseSpec(node_id=node.device_id, at_tick=0, snr_drop_db=float(low) - 29.0)

    found = anomalies(tmp_path, [mild], 17 * 12 + 24)

    assert found.filter(pl.col("signal") == "t3_rate").height >= 1


def test_partial_failure_early_in_a_run_is_still_caught(topo: Topology, tmp_path: Path) -> None:
    # Only two healthy polls exist before tick 6. The RF baseline must work with that.
    amp = max(
        topo.of_type(Amplifier),
        key=lambda a: sum(isinstance(d, Modem) for d in topo.subtree(a.device_id)),
    )
    found = anomalies(
        tmp_path, [AmplifierFailureSpec(amp_id=amp.device_id, at_tick=6, partial=True)], 30
    )

    assert found["signal"].to_list() == ["rf_level_drop"]
    assert found["tick"].to_list() == [6]
