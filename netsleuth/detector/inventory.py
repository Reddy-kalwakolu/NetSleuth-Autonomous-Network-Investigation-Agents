"""Finds each modem's node from the stored inventory.

Plain SQL with a recursive CTE, so the Athena backend can run it too. Detectors append their own
query after this prefix and join on ``modem_node(modem_id, node_id)``.
"""

MODEM_NODE_CTE = """
WITH RECURSIVE up (modem_id, device_id) AS (
    SELECT device_id, parent_id FROM topology_devices WHERE device_type = 'modem'
    UNION ALL
    SELECT up.modem_id, d.parent_id
    FROM up JOIN topology_devices AS d ON d.device_id = up.device_id
    WHERE d.device_type <> 'node'
),
modem_node AS (
    SELECT up.modem_id, up.device_id AS node_id
    FROM up JOIN topology_devices AS n ON n.device_id = up.device_id
    WHERE n.device_type = 'node'
)
"""
