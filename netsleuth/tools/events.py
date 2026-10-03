"""What the event streams say: utility power, power supplies, configuration changes, peering
links and customer tickets, up to as_of.

Every scope argument takes a device, a fiber route or a power area, and most tools also work with
no scope at all, for the whole network. A table that has no rows yet simply means nothing has
happened, so every tool works on a run without these events.
"""

from collections import Counter
from typing import Any

import polars as pl

from netsleuth.storage import StorageSession
from netsleuth.tools.base import ToolError, ToolResult, tool
from netsleuth.tools.inventory import Inventory, inventory

TICKS_PER_HOUR = 12
MINUTES_PER_TICK = 5
MAX_LISTED = 5


def _now_tick(session: StorageSession) -> int:
    """The latest tick of the always present service group table: "now" for every tool here."""
    value = session.query("SELECT max(tick) AS t FROM sg_status")["t"][0]
    return int(value) if value is not None else 0


def _check_scope(inv: Inventory, scope_id: str) -> None:
    if (
        scope_id not in inv.device_type
        and scope_id not in inv.routes
        and (scope_id not in inv.power_areas)
    ):
        raise ToolError(f"no device, fiber route or power area {scope_id}")


def _modems_in(inv: Inventory, scope_id: str | None) -> set[str] | None:
    """Modems in a scope, or None for the whole network."""
    if scope_id is None:
        return None
    _check_scope(inv, scope_id)
    if scope_id in inv.device_type:
        return set(inv.modems_under(scope_id))
    if scope_id in inv.routes:
        return set().union(*(inv.modems_under(n) for n in inv.nodes_on_route(scope_id)))
    return {d for d, a in inv.power_area.items() if a == scope_id and inv.device_type[d] == "modem"}


def _areas_in(inv: Inventory, scope_id: str | None) -> set[str] | None:
    """Power areas a scope touches: its own homes, and the supplies feeding its actives."""
    if scope_id is None:
        return None
    if scope_id in inv.power_areas:
        return {scope_id}
    modems = _modems_in(inv, scope_id) or set()
    areas = {str(inv.power_area[m]) for m in modems if inv.power_area[m]}
    for supply in _supplies_in(inv, scope_id):
        if inv.power_area.get(supply):
            areas.add(str(inv.power_area[supply]))
    return areas


def _supplies_in(inv: Inventory, scope_id: str) -> list[str]:
    """Supplies in a power area, the supply itself, or the supplies feeding a device and the
    actives behind it."""
    if scope_id in inv.power_areas:
        return sorted(d for d, a in inv.power_area.items() if a == scope_id and d in inv.feeds)
    if scope_id in inv.feeds:
        return [scope_id]
    devices = set(inv.nodes_on_route(scope_id)) if scope_id in inv.routes else {scope_id}
    stack = list(devices)
    while stack:
        current = stack.pop()
        for child in inv.children.get(current, []):
            if inv.device_type[child] in ("node", "amplifier") and child not in devices:
                devices.add(child)
                stack.append(child)
    return sorted({inv.supply_of[d] for d in devices if d in inv.supply_of})


def _clock(row_ts: Any) -> str:
    return str(row_ts.strftime("%H:%M"))


@tool
def get_power_events(
    session: StorageSession, scope_id: str | None = None, hours: int = 6
) -> ToolResult:
    inv = inventory(session)
    if scope_id is not None:
        _check_scope(inv, scope_id)
    areas = _areas_in(inv, scope_id)
    since = _now_tick(session) - hours * TICKS_PER_HOUR
    events: list[dict[str, Any]] = []
    out_since: dict[str, Any] = {}
    if "power_events" in session.tables:
        rows = session.query(
            "SELECT ts, tick, power_area, event FROM power_events ORDER BY tick, power_area"
        )
        for row in rows.iter_rows(named=True):
            if areas is not None and row["power_area"] not in areas:
                continue
            if row["event"] == "outage_start":
                out_since[row["power_area"]] = row["ts"]
            else:
                out_since.pop(row["power_area"], None)
            if row["tick"] > since:
                events.append(
                    {
                        "power_area": row["power_area"],
                        "event": row["event"],
                        "at": _clock(row["ts"]),
                    }
                )
    out_now = sorted(out_since)
    where = scope_id or "the network"
    if events or out_now:
        parts = [f"{a} out since {_clock(out_since[a])} (still out)" for a in out_now]
        restored = [e for e in events if e["event"] == "restored"]
        parts += [f"{e['power_area']} restored at {e['at']}" for e in restored]
        summary = f"Utility power for {where}, last {hours} h: " + "; ".join(parts) + "."
    else:
        summary = f"No utility power events for {where} in the last {hours} h."
    return ToolResult(
        tool="get_power_events",
        args={"scope_id": scope_id, "hours": hours},
        summary=summary,
        data={"events": events, "out_now": out_now},
    )


@tool
def get_power_supply_status(session: StorageSession, scope_id: str) -> ToolResult:
    inv = inventory(session)
    _check_scope(inv, scope_id)
    supplies = _supplies_in(inv, scope_id)
    now = _now_tick(session)
    found: list[dict[str, Any]] = []
    latest: dict[str, dict[str, Any]] = {}
    if supplies and "ps_status" in session.tables:
        placeholders = ", ".join("?" for _ in supplies)
        rows = session.query(
            "SELECT tick, ps_id, ac_ok, on_battery, battery_min_left FROM ps_status "
            f"WHERE ps_id IN ({placeholders}) ORDER BY tick",
            supplies,
        )
        for row in rows.iter_rows(named=True):
            latest[row["ps_id"]] = row
    for ps_id in supplies:
        if ps_id not in latest:
            found.append({"ps_id": ps_id, "reported": False})
            continue
        row = latest[ps_id]
        found.append(
            {
                "ps_id": ps_id,
                "reported": True,
                "ac_ok": row["ac_ok"],
                "on_battery": row["on_battery"],
                "last_battery_min_left": round(row["battery_min_left"], 1),
                "silent_min": (now - row["tick"]) * MINUTES_PER_TICK,
            }
        )

    def describe(s: dict[str, Any]) -> str:
        if not s["reported"]:
            return f"{s['ps_id']} has never reported"
        state = (
            "on utility power"
            if s["ac_ok"]
            else f"on battery, {s['last_battery_min_left']:.0f} min left"
        )
        if s["silent_min"]:
            return f"{s['ps_id']} silent for {s['silent_min']} min, last seen {state}"
        return f"{s['ps_id']} {state}"

    summary = (
        f"Power supplies for {scope_id}: " + "; ".join(describe(s) for s in found) + "."
        if found
        else f"No power supply feeds {scope_id}."
    )
    return ToolResult(
        tool="get_power_supply_status",
        args={"scope_id": scope_id},
        summary=summary,
        data={"supplies": found},
    )


@tool
def get_recent_changes(
    session: StorageSession, scope_id: str | None = None, hours: int = 24
) -> ToolResult:
    inv = inventory(session)
    relevant: set[str] | None = None
    if scope_id is not None:
        if scope_id not in inv.device_type:
            raise ToolError(f"no device {scope_id} in the inventory")
        relevant = {scope_id}
        up = inv.parent[scope_id]
        while up is not None:  # a change above the device reaches it
            relevant.add(up)
            up = inv.parent[up]
        relevant |= {
            c for c in inv.children.get(scope_id, []) if inv.device_type[c] == "service_group"
        }
    since = _now_tick(session) - hours * TICKS_PER_HOUR
    changes: list[dict[str, Any]] = []
    if "change_log" in session.tables:
        rows = session.query(
            "SELECT ts, tick, change_id, target_id, description FROM change_log "
            "WHERE tick > ? ORDER BY tick",
            [since],
        )
        for row in rows.iter_rows(named=True):
            if relevant is None or row["target_id"] in relevant:
                changes.append(
                    {
                        "change_id": row["change_id"],
                        "target_id": row["target_id"],
                        "description": row["description"],
                        "at": _clock(row["ts"]),
                        "minutes_ago": (_now_tick(session) - row["tick"]) * MINUTES_PER_TICK,
                    }
                )
    where = scope_id or "the network"
    if changes:
        summary = (
            f"Changes affecting {where}, last {hours} h: "
            + "; ".join(
                f"{c['change_id']} on {c['target_id']} at {c['at']} ({c['description']})"
                for c in changes
            )
            + "."
        )
    else:
        summary = f"No changes affecting {where} in the last {hours} h."
    return ToolResult(
        tool="get_recent_changes",
        args={"scope_id": scope_id, "hours": hours},
        summary=summary,
        data={"changes": changes},
    )


@tool
def get_peering_status(
    session: StorageSession, link_id: str | None = None, hours: int = 1
) -> ToolResult:
    inv = inventory(session)
    if link_id is not None and inv.device_type.get(link_id) != "peering_link":
        raise ToolError(f"no peering link {link_id}")
    links: list[dict[str, Any]] = []
    if "peering_status" in session.tables:
        since = _now_tick(session) - hours * TICKS_PER_HOUR
        rows = session.query(
            "SELECT tick, link_id, util_pct, latency_ms, drop_pct FROM peering_status "
            "WHERE tick > ?",
            [since],
        )
        if link_id is not None:
            rows = rows.filter(pl.col("link_id") == link_id)
        for (lid,), part in sorted(rows.partition_by("link_id", as_dict=True).items()):
            last = part.sort("tick").row(-1, named=True)
            links.append(
                {
                    "link_id": lid,
                    "util_pct": round(last["util_pct"], 1),
                    "latency_ms": round(last["latency_ms"], 1),
                    "drop_pct": round(last["drop_pct"], 2),
                    "peak_util_pct": round(max(part["util_pct"].to_list()), 1),
                }
            )
    if links:
        summary = (
            f"Peering, last {hours} h: "
            + "; ".join(
                f"{x['link_id']} now {x['util_pct']}% ({x['latency_ms']} ms, "
                f"{x['drop_pct']}% drops), peak {x['peak_util_pct']}%"
                for x in links
            )
            + "."
        )
    else:
        summary = "No peering data."
    return ToolResult(
        tool="get_peering_status",
        args={"link_id": link_id, "hours": hours},
        summary=summary,
        data={"links": links},
    )


@tool
def get_tickets(session: StorageSession, scope_id: str | None = None, hours: int = 1) -> ToolResult:
    inv = inventory(session)
    modems = _modems_in(inv, scope_id)
    since = _now_tick(session) - hours * TICKS_PER_HOUR
    by_kind: Counter[str] = Counter()
    by_node: Counter[str] = Counter()
    if "tickets" in session.tables:
        rows = session.query("SELECT modem_id, kind FROM tickets WHERE tick > ?", [since])
        if modems is not None:
            rows = rows.filter(pl.col("modem_id").is_in(list(modems)))
        for modem_id, kind in rows.iter_rows():
            by_kind[kind] += 1
            node = inv.node_of(modem_id) if modem_id in inv.device_type else None
            by_node[node or "unknown"] += 1
    where = scope_id or "the network"
    total = sum(by_kind.values())
    if total:
        summary = (
            f"{total} tickets for {where} in the last {hours} h: "
            + ", ".join(f"{n} {k}" for k, n in sorted(by_kind.items()))
            + ". Busiest nodes: "
            + ", ".join(f"{n} {c}" for n, c in by_node.most_common(MAX_LISTED))
            + "."
        )
    else:
        summary = f"No tickets for {where} in the last {hours} h."
    return ToolResult(
        tool="get_tickets",
        args={"scope_id": scope_id, "hours": hours},
        summary=summary,
        data={"by_kind": dict(sorted(by_kind.items())), "by_node": dict(by_node.most_common())},
    )
