"""What the telemetry says about a device, a service group or a fiber route, up to as_of."""

from statistics import median
from typing import Any, cast

import polars as pl

from netsleuth.storage import StorageSession
from netsleuth.tools.base import ToolError, ToolResult, tool
from netsleuth.tools.inventory import Inventory, inventory, offline_now

TICKS_PER_HOUR = 12
DROP_DB = 4.0
MAX_POINTS = 24

# metric: (table, id column, value expression, device types it applies to)
METRICS: dict[str, tuple[str, str, str, tuple[str, ...]]] = {
    "ds_rx_power_dbmv": ("cm_rf", "modem_id", "ds_rx_power_dbmv", ("modem",)),
    "ds_mer_db": ("cm_rf", "modem_id", "ds_mer_db", ("modem",)),
    "us_tx_power_dbmv": ("cm_rf", "modem_id", "us_tx_power_dbmv", ("modem",)),
    "us_rx_mer_db": ("cm_status", "modem_id", "us_rx_mer_db", ("modem",)),
    "optical_rx_dbm": ("node_optical", "node_id", "optical_rx_dbm", ("node",)),
    "temperature_c": ("node_optical", "node_id", "temperature_c", ("node",)),
    "us_util_pct": ("sg_status", "sg_id", "us_util_pct", ("service_group",)),
    "ds_util_pct": ("sg_status", "sg_id", "ds_util_pct", ("service_group",)),
}


def _modems_in(inv: Inventory, scope_id: str) -> frozenset[str]:
    if scope_id in inv.device_type:
        return inv.modems_under(scope_id)
    if scope_id in inv.routes:
        return frozenset().union(*(inv.modems_under(n) for n in inv.nodes_on_route(scope_id)))
    raise ToolError(f"no device or fiber route {scope_id}")


def _num(value: object) -> float:
    """A polars scalar as a float. polars types its aggregates loosely."""
    return float(cast(Any, value))


def _latest_tick(session: StorageSession, table: str) -> int:
    value = session.query(f"SELECT max(tick) AS t FROM {table}")["t"][0]
    return int(value) if value is not None else 0


@tool
def get_anomaly(session: StorageSession, anomaly_id: str) -> ToolResult:
    if "anomaly_events" not in session.tables:
        raise ToolError("no anomalies recorded yet")
    rows = session.query("SELECT * FROM anomaly_events WHERE anomaly_id = ?", [anomaly_id])
    if rows.is_empty():
        raise ToolError(f"no anomaly {anomaly_id}")
    row = rows.row(0, named=True)
    data = {
        k: (v.isoformat() if hasattr(v, "isoformat") else v)
        for k, v in row.items()
        if v is not None
    }
    summary = (
        f"{row['signal']} on {row['scope_device_id']} at tick {row['tick']}: "
        f"value {row['value']:.3f} against a baseline of {row['baseline']:.3f}."
    )
    return ToolResult(
        tool="get_anomaly", args={"anomaly_id": anomaly_id}, summary=summary, data=data
    )


@tool
def summarize_modem_health(session: StorageSession, scope_id: str, hours: int = 1) -> ToolResult:
    inv = inventory(session)
    modems = _modems_in(inv, scope_id)
    ids = list(modems)
    status_tick = _latest_tick(session, "cm_status")
    then_tick = max(0, status_tick - hours * TICKS_PER_HOUR)
    offline = offline_now(session) & modems
    before = (
        set(
            session.query(
                "SELECT modem_id FROM cm_status WHERE tick = ? AND NOT online", [then_tick]
            )["modem_id"]
        )
        & modems
    )

    rf = session.query(
        "SELECT modem_id, tick, ds_rx_power_dbmv FROM cm_rf WHERE tick >= ?",
        [then_tick],
    ).filter(pl.col("modem_id").is_in(ids))
    polled_tick = int(rf["tick"].max()) if not rf.is_empty() else -1  # type: ignore[arg-type]
    now = rf.filter(pl.col("tick") == polled_tick)
    earlier = (
        rf.filter(pl.col("tick") < polled_tick)
        .group_by("modem_id")
        .agg(pl.col("ds_rx_power_dbmv").median().alias("before"))
    )
    joined = now.join(earlier, on="modem_id", how="inner").with_columns(
        (pl.col("before") - pl.col("ds_rx_power_dbmv")).alias("drop")
    )
    polled = set(now["modem_id"])
    silent_online = (modems - offline) - polled
    dropped = joined.filter(pl.col("drop") >= DROP_DB)
    worst = joined.sort("drop", descending=True).head(5)["modem_id"].to_list()
    t3 = 0
    if "cm_events" in session.tables:
        t3_rows = session.query(
            "SELECT DISTINCT modem_id FROM cm_events WHERE event = 'T3' AND tick > ?", [then_tick]
        )
        t3 = len(set(t3_rows["modem_id"]) & modems)
    median_drop = float(median(joined["drop"].to_list())) if joined.height else 0.0
    data: dict[str, Any] = {
        "modems": len(modems),
        "offline_now": len(offline),
        "offline_before": len(before),
        "polled_now": len(polled),
        "silent_but_online": len(silent_online),
        "t3_modems": t3,
        "dropped_4db": dropped.height,
        "median_ds_drop_db": round(median_drop, 2),
        "worst": worst,
    }
    summary = (
        f"{scope_id}: {len(offline)} of {len(modems)} modems offline now, {len(before)} "
        f"{hours} h ago. {len(polled)} answered the latest RF poll. {dropped.height} lost 4 dB or "
        f"more of downstream power (median change {median_drop:+.1f} dB). {t3} logged T3 in the "
        "last hour."
    )
    return ToolResult(
        tool="summarize_modem_health",
        args={"scope_id": scope_id, "hours": hours},
        summary=summary,
        data=data,
    )


@tool
def get_metric_series(
    session: StorageSession, device_id: str, metric: str, hours: int = 2
) -> ToolResult:
    inv = inventory(session)
    if device_id not in inv.device_type:
        raise ToolError(f"no device {device_id} in the inventory")
    if metric not in METRICS:
        raise ToolError(f"unknown metric {metric}; use one of {', '.join(METRICS)}")
    table, id_col, column, kinds = METRICS[metric]
    if inv.device_type[device_id] not in kinds:
        raise ToolError(f"{device_id} is a {inv.device_type[device_id]}, which has no {metric}")
    as_of_tick = _latest_tick(session, "sg_status")
    since = max(0, as_of_tick - hours * TICKS_PER_HOUR)
    rows = session.query(
        f"SELECT tick, {column} AS value FROM {table} "
        f"WHERE {id_col} = ? AND tick >= ? ORDER BY tick",
        [device_id, since],
    ).drop_nulls("value")
    step = max(1, rows.height // MAX_POINTS)
    points = [[int(t), round(float(v), 2)] for t, v in rows.gather_every(step).iter_rows()]
    last_tick = int(rows["tick"].max()) if rows.height else since - 1  # type: ignore[arg-type]
    silent = as_of_tick - last_tick
    if rows.height:
        values = rows["value"]
        summary = (
            f"{metric} for {device_id} over {hours} h: from {values[0]:.2f} to {values[-1]:.2f} "
            f"(min {_num(values.min()):.2f}, max {_num(values.max()):.2f})."
        )
    else:
        summary = f"No {metric} reported by {device_id} in the last {hours} h."
    if silent > 3:
        summary += f" Nothing reported for the last {silent} ticks."
    return ToolResult(
        tool="get_metric_series",
        args={"device_id": device_id, "metric": metric, "hours": hours},
        summary=summary,
        data={
            "points": points,
            "last_tick": last_tick,
            "as_of_tick": as_of_tick,
            "silent_ticks": max(0, silent),
        },
    )


@tool
def get_cm_events(session: StorageSession, scope_id: str, hours: int = 1) -> ToolResult:
    inv = inventory(session)
    modems = _modems_in(inv, scope_id)
    data: dict[str, Any] = {"t3_events": 0, "t4_events": 0, "modems_with_t3": 0, "by_node": {}}
    if "cm_events" in session.tables:
        since = _latest_tick(session, "sg_status") - hours * TICKS_PER_HOUR
        rows = session.query(
            "SELECT modem_id, event FROM cm_events WHERE tick > ?", [since]
        ).filter(pl.col("modem_id").is_in(list(modems)))
        t3 = rows.filter(pl.col("event") == "T3")
        data["t3_events"] = t3.height
        data["t4_events"] = rows.height - t3.height
        data["modems_with_t3"] = t3["modem_id"].n_unique()
        node_of = {
            m: n
            for n in inv.device_type
            if inv.device_type[n] == "node"
            for m in inv.modems_under(n)
        }
        by_node: dict[str, int] = {}
        for m in set(t3["modem_id"]):
            by_node[node_of[m]] = by_node.get(node_of[m], 0) + 1
        data["by_node"] = dict(sorted(by_node.items()))
    summary = (
        f"{scope_id}, last {hours} h: {data['t3_events']} T3 timeouts from "
        f"{data['modems_with_t3']} modems, {data['t4_events']} T4 resets."
    )
    if data["by_node"]:
        summary += (
            " T3 modems by node: " + ", ".join(f"{n} {c}" for n, c in data["by_node"].items()) + "."
        )
    return ToolResult(
        tool="get_cm_events",
        args={"scope_id": scope_id, "hours": hours},
        summary=summary,
        data=data,
    )


@tool
def get_service_group_health(session: StorageSession, device_id: str) -> ToolResult:
    inv = inventory(session)
    if device_id not in inv.device_type:
        raise ToolError(f"no device {device_id} in the inventory")
    sg = device_id
    while inv.device_type[sg] != "service_group":
        parent = inv.parent[sg]
        if parent is None:
            raise ToolError(f"{device_id} is not inside a service group")
        sg = parent
    now_tick = _latest_tick(session, "sg_channels")
    rows = session.query(
        "SELECT tick, channel, us_snr_db, us_uncorrectable_cw_total FROM sg_channels "
        "WHERE sg_id = ? AND tick >= ? ORDER BY channel, tick",
        [sg, max(0, now_tick - TICKS_PER_HOUR)],
    )
    channels = []
    for (channel,), part in rows.group_by("channel", maintain_order=True):
        now = part.filter(pl.col("tick") == now_tick)
        earlier = part.filter(pl.col("tick") < now_tick)
        channels.append(
            {
                "channel": channel,
                "snr_now": round(float(now["us_snr_db"][0]), 1) if now.height else None,
                "snr_before": (
                    round(_num(earlier["us_snr_db"].median()), 1) if earlier.height else None
                ),
                "uncorrectable_increase": int(
                    _num(part["us_uncorrectable_cw_total"].max())
                    - _num(part["us_uncorrectable_cw_total"].min())
                ),
            }
        )
    status = session.query(
        "SELECT modems_online, modems_total FROM sg_status WHERE sg_id = ? AND tick = ?",
        [sg, now_tick],
    )
    online, total = (
        (int(status["modems_online"][0]), int(status["modems_total"][0]))
        if status.height
        else (0, 0)
    )
    low = [c for c in channels if c["channel"] in ("us1", "us2")]
    uncorrectable = sum(c["uncorrectable_increase"] for c in channels)
    snr_text = ", ".join(f"{c['channel']} {c['snr_now']} dB (was {c['snr_before']})" for c in low)
    summary = (
        f"{sg}: {online} of {total} modems online. Low channel SNR {snr_text}. Uncorrectable "
        f"upstream codewords in the last hour: {uncorrectable}."
    )
    return ToolResult(
        tool="get_service_group_health",
        args={"device_id": device_id},
        summary=summary,
        data={
            "service_group": sg,
            "channels": channels,
            "modems_online": online,
            "modems_total": total,
        },
    )


@tool
def get_maintenance_windows(session: StorageSession, scope_id: str) -> ToolResult:
    inv = inventory(session)
    if scope_id not in inv.device_type:
        raise ToolError(f"no device {scope_id} in the inventory")
    scopes = [scope_id]
    up = inv.parent[scope_id]
    while up is not None:
        scopes.append(up)
        up = inv.parent[up]
    windows: list[dict[str, Any]] = []
    if "maintenance" in session.tables:
        placeholders = ", ".join("?" for _ in scopes)
        rows = session.query(
            f"SELECT window_id, scope_device_id, starts_at, ends_at FROM maintenance "
            f"WHERE scope_device_id IN ({placeholders}) AND ends_at > ? ORDER BY starts_at",
            [*scopes, session.as_of],
        )
        for row in rows.iter_rows(named=True):
            windows.append(
                {
                    "window_id": row["window_id"],
                    "scope_device_id": row["scope_device_id"],
                    "starts_at": row["starts_at"].isoformat(),
                    "ends_at": row["ends_at"].isoformat(),
                    "active": row["starts_at"] <= session.as_of,
                }
            )
    if windows:
        summary = (
            "; ".join(
                f"{w['window_id']} on {w['scope_device_id']} "
                f"from {w['starts_at']} to {w['ends_at']}"
                + (" (active now)" if w["active"] else " (upcoming)")
                for w in windows
            )
            + "."
        )
    else:
        summary = f"No active or upcoming planned work on {scope_id} or anything above it."
    return ToolResult(
        tool="get_maintenance_windows",
        args={"scope_id": scope_id},
        summary=summary,
        data={"windows": windows},
    )
