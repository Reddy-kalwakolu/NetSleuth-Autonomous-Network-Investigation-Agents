"""The rules baseline: plain decision logic over the same data the agent gets.

It sees only a storage session cut off at the moment the anomaly was detected. It never sees the
ground truth or the true topology, only the stored inventory.

The rule is the one field engineers use for outages: find the highest device whose entire
downstream is dark. Start from the lowest common ancestor of the offline modems on the flagged
node, and keep walking up while everything under the next device up is offline too.

Known blind spot: if amplifier A has no taps of its own and feeds only amplifier B, a failure of B
looks exactly like a failure of A, and the rule blames A. Partial location credit covers that case,
and the amplifiers that report their own telemetry can break the tie later.
"""

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from netsleuth.diagnosis import Diagnosis, DiagnosisCategory
from netsleuth.storage import StorageSession

CATEGORY_BY_DEVICE_TYPE: dict[str, DiagnosisCategory] = {
    "amplifier": "amplifier_failure",
    "node": "fiber_cut",
}


def rules_baseline(session: StorageSession, anomaly: Mapping[str, Any]) -> Diagnosis:
    incident_id = str(anomaly["anomaly_id"])
    node_id = str(anomaly["scope_device_id"])

    inventory = session.query("SELECT device_id, device_type, parent_id FROM topology_devices")
    parent: dict[str, str | None] = dict(
        zip(inventory["device_id"], inventory["parent_id"], strict=True)
    )
    device_type: dict[str, str] = dict(
        zip(inventory["device_id"], inventory["device_type"], strict=True)
    )
    children: dict[str, list[str]] = defaultdict(list)
    for device_id, parent_id in parent.items():
        if parent_id is not None:
            children[parent_id].append(device_id)

    modem_cache: dict[str, frozenset[str]] = {}

    def modems_under(device_id: str) -> frozenset[str]:
        if device_id not in modem_cache:
            if device_type[device_id] == "modem":
                found = frozenset({device_id})
            else:
                found = frozenset().union(*(modems_under(c) for c in children[device_id]))
            modem_cache[device_id] = found
        return modem_cache[device_id]

    offline = set(
        session.query(
            "SELECT modem_id FROM cm_status "
            "WHERE ts = (SELECT max(ts) FROM cm_status) AND NOT online"
        )["modem_id"]
    )
    dark = offline & modems_under(node_id)
    if not dark:
        return Diagnosis(
            incident_id=incident_id,
            root_cause_category="insufficient_evidence",
            root_cause_device_id=None,
            summary=f"No modems on {node_id} are offline.",
        )

    root = _lowest_common_ancestor(dark, parent)
    while root != node_id:
        up = parent[root]
        if up is None or not modems_under(up) <= offline:
            break
        root = up

    category = CATEGORY_BY_DEVICE_TYPE.get(device_type[root], "unknown")
    affected = tuple(sorted(modems_under(root) & offline))
    return Diagnosis(
        incident_id=incident_id,
        root_cause_category=category,
        root_cause_device_id=root,
        affected_device_ids=affected,
        summary=f"All {len(affected)} modems behind {root} are offline, and nothing above is.",
    )


def _lowest_common_ancestor(devices: set[str], parent: Mapping[str, str | None]) -> str:
    def chain(device_id: str) -> list[str]:
        path = [device_id]
        while (up := parent[path[-1]]) is not None:
            path.append(up)
        return path

    first, *rest = devices
    shared = set(chain(first))
    for device_id in rest:
        shared &= set(chain(device_id))
    return next(d for d in chain(first) if d in shared)
