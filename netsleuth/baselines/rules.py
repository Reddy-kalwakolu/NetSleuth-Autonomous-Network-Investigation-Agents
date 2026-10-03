"""The rules baseline: plain decision logic over the same data the agent gets.

It sees only a storage session cut off at the moment the anomaly was detected. It never sees the
ground truth or the true topology, only the stored inventory. It checks, in this order:

1. A peering link near full. Peering congestion at that link: a backbone problem, not the plant.
2. Planned work. If a published maintenance window covers the node right now, it is planned
   maintenance and nobody should be sent.
3. Utility power. If an area is out right now and a power supply that feeds this node has run out
   of battery, that supply. Otherwise, if most of the dark modems have no power at home, a
   commercial power outage in the area with the most of them.
4. Outages (share offline). The rule field engineers use: find the highest device whose entire
   downstream is dark, starting from the lowest common ancestor of the offline modems. If the whole
   node is dark and another node on the same fiber route is dark too, the route is cut.
5. Upstream trouble (SNR drop or T3 rate). If a change went into the service group or its CMTS in
   the last hour, that change. Otherwise ingress, called at the node with the most modems logging
   T3 timeouts in the last hour.
6. Level drops (RF). The outage rule again, applied to modems whose downstream power fell 4 dB or
   more, which pins a partial amplifier failure.

Known blind spot: if amplifier A has no taps of its own and feeds only amplifier B, a failure of B
looks exactly like a failure of A, and the rules blame A. Partial location credit covers that case,
and the amplifiers that report their own telemetry can break the tie later.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

import polars as pl

from netsleuth.diagnosis import Diagnosis, DiagnosisCategory
from netsleuth.storage import StorageSession
from netsleuth.tools.inventory import Inventory, offline_now
from netsleuth.tools.inventory import inventory as load_inventory

CATEGORY_BY_DEVICE_TYPE: dict[str, DiagnosisCategory] = {
    "amplifier": "amplifier_failure",
    "node": "fiber_cut",
}

RF_DROP_DB = 4.0
RF_BASELINE_POLLS = 4
RF_POLL_TICKS = 3
T3_WINDOW_TICKS = 12
CHANGE_WINDOW_TICKS = 12
MINUTES_PER_TICK = 5


def rules_baseline(session: StorageSession, anomaly: Mapping[str, Any]) -> Diagnosis:
    incident_id = str(anomaly["anomaly_id"])
    scope = str(anomaly["scope_device_id"])
    signal = str(anomaly.get("signal", "share_offline"))
    inventory = load_inventory(session)
    if signal == "peering_util":
        return Diagnosis(
            incident_id=incident_id,
            root_cause_category="peering_congestion",
            root_cause_device_id=scope,
            summary=f"Peering link {scope} is running near full.",
        )
    if inventory.device_type[scope] == "node":
        node_ids = [scope]
    else:
        node_ids = [c for c in inventory.children[scope] if inventory.device_type[c] == "node"]

    # Only planned work on the anomaly's own node, or on its whole service group, explains it.
    # A sibling's window says nothing about this node.
    own_scopes = [scope]
    if inventory.device_type[scope] == "node" and (sg := inventory.parent[scope]) is not None:
        own_scopes.append(sg)
    window = _active_window(session, own_scopes, session.as_of)
    if window is not None:
        window_id, window_scope = window
        return Diagnosis(
            incident_id=incident_id,
            root_cause_category="planned_maintenance",
            root_cause_device_id=window_scope,
            summary=f"Maintenance window {window_id} covers {window_scope} right now.",
        )
    if signal in ("sg_snr", "t3_rate"):
        change = _recent_change(session, inventory, scope)
        if change is not None:
            change_id, target = change
            return Diagnosis(
                incident_id=incident_id,
                root_cause_category="config_change",
                root_cause_device_id=target,
                summary=f"Upstream got worse right after change {change_id} on {target}.",
            )
        return _ingress(session, inventory, incident_id, node_ids)
    if signal == "rf_level_drop":
        return _partial(session, inventory, incident_id, scope)
    power = _power(session, inventory, incident_id, scope)
    if power is not None:
        return power
    return _outage(session, inventory, incident_id, scope)


def _recent_change(
    session: StorageSession, inventory: Inventory, scope: str
) -> tuple[str, str] | None:
    """The latest change in the last hour on the scope, its service group or its CMTS."""
    if "change_log" not in session.tables:
        return None
    above = {scope}
    up = inventory.parent[scope]
    while up is not None:
        above.add(up)
        up = inventory.parent[up]
    rows = session.query(
        "SELECT change_id, target_id FROM change_log "
        f"WHERE tick > (SELECT max(tick) FROM sg_status) - {CHANGE_WINDOW_TICKS} "
        "ORDER BY tick DESC"
    )
    for change_id, target in rows.iter_rows():
        if target in above:
            return str(change_id), str(target)
    return None


def _areas_out(session: StorageSession) -> set[str]:
    if "power_events" not in session.tables:
        return set()
    rows = session.query("SELECT power_area, event FROM power_events ORDER BY tick")
    out: set[str] = set()
    for area, event in rows.iter_rows():
        if event == "outage_start":
            out.add(area)
        else:
            out.discard(area)
    return out


def _power(
    session: StorageSession, inventory: Inventory, incident_id: str, node_id: str
) -> Diagnosis | None:
    """A utility outage behind this node's darkness, if there is one."""
    out = _areas_out(session)
    if not out:
        return None
    node_actives = {
        d for d in [node_id, *_actives_under(inventory, node_id)] if d in inventory.supply_of
    }
    supplies = {inventory.supply_of[a] for a in node_actives}
    drained = sorted(
        s for s in supplies if inventory.power_area.get(s) in out and _drained(session, s)
    )
    if drained:
        return Diagnosis(
            incident_id=incident_id,
            root_cause_category="power_supply_failure",
            root_cause_device_id=drained[0],
            summary=f"{drained[0]} ran out of battery during a utility outage.",
        )
    dark = offline_now(session) & inventory.modems_under(node_id)
    unpowered = [m for m in dark if inventory.power_area.get(m) in out]
    if dark and len(unpowered) * 2 >= len(dark):
        counts: dict[str, int] = {}
        for m in unpowered:
            area = str(inventory.power_area[m])
            counts[area] = counts.get(area, 0) + 1
        area = max(sorted(counts), key=lambda a: counts[a])
        return Diagnosis(
            incident_id=incident_id,
            root_cause_category="commercial_power_outage",
            root_cause_device_id=area,
            summary=f"{len(unpowered)} of the {len(dark)} dark modems are in {area}, "
            "which has no utility power.",
        )
    return None


def _actives_under(inventory: Inventory, device_id: str) -> list[str]:
    found = []
    stack = list(inventory.children.get(device_id, []))
    while stack:
        current = stack.pop()
        if inventory.device_type[current] == "amplifier":
            found.append(current)
            stack.extend(inventory.children.get(current, []))
    return found


def _drained(session: StorageSession, supply: str) -> bool:
    """A supply that has run out of battery. Its transponder dies with the actives it powers, so
    a drained supply is usually silent, but silence alone isn't enough: a failure upstream of it
    silences it too. It counts as drained only if its last report was on battery with less charge
    left than the time it has been quiet since."""
    if "ps_status" not in session.tables:
        return False
    rows = session.query(
        "SELECT tick, on_battery, battery_min_left FROM ps_status WHERE ps_id = ? "
        "ORDER BY tick DESC LIMIT 1",
        [supply],
    )
    if rows.is_empty():
        return False
    last = rows.row(0, named=True)
    now = session.query("SELECT max(tick) AS t FROM sg_status")["t"][0]
    quiet_min = (int(now) - int(last["tick"])) * MINUTES_PER_TICK
    return bool(last["on_battery"]) and last["battery_min_left"] <= quiet_min + MINUTES_PER_TICK


def _active_window(
    session: StorageSession, scopes: list[str], as_of: datetime
) -> tuple[str, str] | None:
    """A published window covering one of the scopes at ``as_of``. Start inclusive, end
    exclusive. A run with no calendar has no windows."""
    if "maintenance" not in session.tables:
        return None
    placeholders = ", ".join("?" for _ in scopes)
    rows = session.query(
        f"SELECT window_id, scope_device_id FROM maintenance "
        f"WHERE scope_device_id IN ({placeholders}) "
        "AND starts_at <= ? AND ends_at > ?",
        [*scopes, as_of, as_of],
    )
    if rows.is_empty():
        return None
    return str(rows["window_id"][0]), str(rows["scope_device_id"][0])


def _outage(
    session: StorageSession, inventory: Inventory, incident_id: str, node_id: str
) -> Diagnosis:
    offline = offline_now(session)
    dark = offline & inventory.modems_under(node_id)
    if not dark:
        return Diagnosis(
            incident_id=incident_id,
            root_cause_category="insufficient_evidence",
            root_cause_device_id=None,
            summary=f"No modems on {node_id} are offline.",
        )
    root = inventory.highest_fully_affected(dark, node_id, set(inventory.modems_under(node_id)))
    if root == node_id:
        route = inventory.fiber_route.get(node_id)
        others_on_route = [
            n
            for n, r in inventory.fiber_route.items()
            if r == route and n != node_id and inventory.device_type[n] == "node"
        ]
        if route and any(
            inventory.modems_under(n) and inventory.modems_under(n) <= offline
            for n in others_on_route
        ):
            return Diagnosis(
                incident_id=incident_id,
                root_cause_category="fiber_cut",
                root_cause_device_id=route,
                affected_device_ids=tuple(sorted(offline)),
                summary=f"{node_id} and other nodes on {route} went dark together.",
            )
    affected = tuple(sorted(inventory.modems_under(root) & offline))
    return Diagnosis(
        incident_id=incident_id,
        root_cause_category=CATEGORY_BY_DEVICE_TYPE.get(inventory.device_type[root], "unknown"),
        root_cause_device_id=root,
        affected_device_ids=affected,
        summary=f"All {len(affected)} modems behind {root} are offline, and nothing above is.",
    )


def _ingress(
    session: StorageSession, inventory: Inventory, incident_id: str, node_ids: list[str]
) -> Diagnosis:
    counts = dict.fromkeys(node_ids, 0)
    if "cm_events" in session.tables:
        recent = session.query(
            "SELECT DISTINCT modem_id FROM cm_events WHERE event = 'T3' "
            f"AND tick > (SELECT max(tick) FROM sg_status) - {T3_WINDOW_TICKS}"
        )
        logged = set(recent["modem_id"])
        counts = {n: len(inventory.modems_under(n) & logged) for n in node_ids}
    node = max(sorted(counts), key=lambda n: counts[n])
    return Diagnosis(
        incident_id=incident_id,
        root_cause_category="ingress_noise",
        root_cause_device_id=node,
        summary=f"Upstream noise on the service group, with the most T3 timeouts on {node}.",
    )


def _partial(
    session: StorageSession, inventory: Inventory, incident_id: str, node_id: str
) -> Diagnosis:
    lookback = RF_POLL_TICKS * (RF_BASELINE_POLLS + 1)
    rf = session.query(
        "SELECT modem_id, tick, ds_rx_power_dbmv FROM cm_rf "
        f"WHERE tick >= (SELECT max(tick) FROM cm_rf) - {lookback}"
    )
    latest = rf["tick"].max()
    now = rf.filter(pl.col("tick") == latest).select(
        "modem_id", pl.col("ds_rx_power_dbmv").alias("now")
    )
    before = (
        rf.filter(pl.col("tick") < latest)
        .group_by("modem_id")
        .agg(pl.col("ds_rx_power_dbmv").median().alias("before"))
    )
    per_modem = now.join(before, on="modem_id", how="inner")
    polled = set(per_modem["modem_id"]) & inventory.modems_under(node_id)
    dropped = (
        set(per_modem.filter(pl.col("before") - pl.col("now") >= RF_DROP_DB)["modem_id"]) & polled
    )
    if not dropped:
        return Diagnosis(
            incident_id=incident_id,
            root_cause_category="insufficient_evidence",
            root_cause_device_id=None,
            summary=f"No modems on {node_id} lost 4 dB of downstream power.",
        )
    root = inventory.highest_fully_affected(dropped, node_id, polled)
    return Diagnosis(
        incident_id=incident_id,
        root_cause_category=CATEGORY_BY_DEVICE_TYPE.get(inventory.device_type[root], "unknown"),
        root_cause_device_id=root,
        affected_device_ids=tuple(sorted(dropped)),
        summary=f"Downstream power fell 4 dB or more on every polled modem behind {root}.",
    )
