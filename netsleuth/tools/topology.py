"""Where a device sits, what hangs off it, and what shares a route, area, model or line card."""

from collections import Counter
from typing import Any

from netsleuth.storage import StorageSession
from netsleuth.tools.base import ToolError, ToolResult, tool
from netsleuth.tools.inventory import inventory, offline_now

SEARCHABLE = ("fiber_route", "power_area", "model", "firmware", "line_card")
MAX_LISTED = 30


def _require(session: StorageSession, device_id: str) -> None:
    if device_id not in inventory(session).device_type:
        raise ToolError(f"no device {device_id} in the inventory")


@tool
def get_device(session: StorageSession, device_id: str) -> ToolResult:
    _require(session, device_id)
    row = session.query("SELECT * FROM topology_devices WHERE device_id = ?", [device_id]).row(
        0, named=True
    )
    data: dict[str, Any] = {
        k: v for k, v in row.items() if v is not None and k not in ("x_km", "y_km")
    }
    inv = inventory(session)
    if data["device_type"] == "modem":
        data["online"] = device_id not in offline_now(session)
        state = "online" if data["online"] else "offline"
    else:
        modems = inv.modems_under(device_id)
        dark = len(modems & offline_now(session))
        data["modems_below"] = len(modems)
        data["modems_offline"] = dark
        state = f"{dark} of {len(modems)} modems below it offline"
    extras = ", ".join(f"{k} {data[k]}" for k in SEARCHABLE if k in data)
    summary = f"{device_id} is a {data['device_type']} under {data.get('parent_id')}; {state}."
    if extras:
        summary += f" {extras}."
    return ToolResult(tool="get_device", args={"device_id": device_id}, summary=summary, data=data)


@tool
def get_ancestors(session: StorageSession, device_id: str) -> ToolResult:
    _require(session, device_id)
    inv = inventory(session)
    chain = []
    up = inv.parent[device_id]
    while up is not None:
        chain.append(up)
        up = inv.parent[up]
    summary = f"{device_id} sits under " + ", then ".join(chain) + "."
    return ToolResult(
        tool="get_ancestors",
        args={"device_id": device_id},
        summary=summary,
        data={"ancestors": chain},
    )


@tool
def get_subtree(session: StorageSession, device_id: str, depth: int = 1) -> ToolResult:
    _require(session, device_id)
    if not 1 <= depth <= 6:
        raise ToolError("depth must be between 1 and 6")
    inv = inventory(session)
    level, seen = [device_id], []
    for _ in range(depth):
        level = [c for d in level for c in inv.children[d]]
        seen.extend(level)
    counts = Counter(inv.device_type[d] for d in seen if inv.device_type[d] != "modem")
    children = inv.children[device_id][:MAX_LISTED]
    modems = len(inv.modems_under(device_id))
    parts = ", ".join(f"{n} {t}" for t, n in sorted(counts.items()))
    summary = (
        f"Within {depth} level(s) below {device_id}: {parts or 'no plant devices'}; "
        f"{modems} modems in total."
    )
    return ToolResult(
        tool="get_subtree",
        args={"device_id": device_id, "depth": depth},
        summary=summary,
        data={"children": children, "counts": dict(counts), "modems_below": modems},
    )


@tool
def find_devices(session: StorageSession, attr: str, value: str) -> ToolResult:
    if attr not in SEARCHABLE:
        raise ToolError(f"can't search by {attr}; use one of {', '.join(SEARCHABLE)}")
    rows = session.query(
        f"SELECT device_id, device_type FROM topology_devices WHERE CAST({attr} AS VARCHAR) = ?",
        [value],
    )
    ids = sorted(rows["device_id"])
    counts = Counter(rows["device_type"])
    listed = ids[:MAX_LISTED]
    more = f" (showing {MAX_LISTED})" if len(ids) > MAX_LISTED else ""
    summary = (
        f"{len(ids)} devices have {attr} {value}: "
        + ", ".join(f"{n} {t}" for t, n in sorted(counts.items()))
        + more
        + "."
    )
    return ToolResult(
        tool="find_devices",
        args={"attr": attr, "value": value},
        summary=summary,
        data={"device_ids": listed, "total": len(ids)},
    )
