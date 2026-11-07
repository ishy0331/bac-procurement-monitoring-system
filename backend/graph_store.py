"""
backend/graph_store.py - READ-ONLY access to the Neo4j graph projection written by graph_sync.py.

Used by GET /api/graph/neo4j (graph_api.py; signed-in BAC staff only).

What it returns: the snapshot of the approved sample records that the last successful
synchronization copied from MariaDB into Neo4j. It is NOT live MariaDB data: changes made in
MariaDB after the last sync are not in it until the next sync.

Ownership (the same markers graph_sync.py writes; nothing else in the Neo4j database is read):
  * nodes:          label BacPmsGraph AND syncOwner = 'santa-rosa-bac-pms'
  * relationships:  syncOwner = 'santa-rosa-bac-pms' AND both end nodes owned as above
  * sync metadata:  (:BacPmsSyncInfo {syncKey: 'bac-pms-graph', syncOwner: 'santa-rosa-bac-pms'})
                    lastRunId, lastSyncedAt, lastSummary (JSON: totals, verificationOk)

Read-only:
  * the three queries are fixed, application-controlled constants; every value is a query parameter,
    nothing from the request is ever put into a query;
  * the session is opened in READ access mode and the queries run in session.execute_read
    (read routing; Neo4j rejects writes in a read transaction).

Sync metadata (stored by graph_sync.py on the BacPmsSyncInfo node; nothing is calculated here):
  * lastRunId     - stored: the run ID (uuid4 hex) of the last sync run that reached its final step
  * lastSyncedAt  - stored: that run's finish time (UTC, ISO 8601)
  * lastSummary   - stored: JSON text {"totals": {...}, "verificationOk": true|false} written by the
                    same run; totals and verificationOk are read from it unchanged (JSON-decoded only).
                    If the text is missing or unreadable, they are returned as null - never assumed.

Node IDs: "<Label>:<MariaDB primary key>" (for example Procurement:101), from the key property
graph_sync.py stores (procurementId, supplierId, ...).

Relationship IDs: "<TYPE>:<source table>:<primary key of the source record>", where the source record
is the MariaDB row that holds the foreign key the relationship was created from (REL_SOURCE_RECORD).
That row's primary key is always the key of one end node (or of both, for supplier_representatives,
whose primary key is (supplier_id, person_id)). Unique because one row holds one value per foreign-key
column, so a row yields at most one relationship of each type, and the type is part of the ID.
Examples: AWARDS:award_notices:701, SUBMITTED:bids:501, REPRESENTED_BY:supplier_representatives:301,401.

Neo4j's internal elementId is used only inside this module to connect relationships to their nodes; it is
never returned. A node with an unknown label or without its key, and a relationship whose type or end
nodes do not fit the schema, are still returned with stableId false and an "unmapped..." ID.
"""

import json
import logging
from datetime import date, datetime

import neo4j_db
from graph_sync import GRAPH_SOURCES, INFO_KEY, INFO_LABEL, NODE_KEYS, OWNER, OWNER_LABEL, REL_SOURCES, REL_TYPES

_log = logging.getLogger(__name__)

DATA_LABEL = "Synced City Sample Data"
NOTICE = ("Snapshot of the system's fictional sample procurement records (data_source = sample), copied from "
          "MariaDB into Neo4j by the last completed synchronization. It is not live data: changes made in "
          "MariaDB after that sync are not shown until the next sync. These are not Santa Rosa City procurement "
          "records and not PhilGEPS reference data.")
MARKER_PROPS = {"syncOwner", "syncRunId"}            # ownership bookkeeping written by graph_sync.py; not returned

READ_INFO = (f"MATCH (i:`{INFO_LABEL}` {{syncKey: $infoKey}}) WHERE i.syncOwner = $owner "
             f"RETURN i.lastRunId AS lastRunId, i.lastSyncedAt AS lastSyncedAt, i.lastSummary AS lastSummary")
READ_NODES = (f"MATCH (n:`{OWNER_LABEL}`) WHERE n.syncOwner = $owner "
              f"RETURN elementId(n) AS eid, labels(n) AS labels, properties(n) AS props")
READ_RELS = (f"MATCH (a:`{OWNER_LABEL}`)-[r]->(b:`{OWNER_LABEL}`) "
             f"WHERE r.syncOwner = $owner AND a.syncOwner = $owner AND b.syncOwner = $owner "
             f"RETURN type(r) AS relType, elementId(a) AS srcEid, elementId(b) AS dstEid, properties(r) AS props")
READ_QUERIES = (READ_INFO, READ_NODES, READ_RELS)

# Relationship type -> (MariaDB table that holds the foreign key, which end node's key is that row's primary key).
# "both": supplier_representatives has the composite primary key (supplier_id, person_id).
REL_SOURCE_RECORD = {
    "PROCURED_BY": ("procurements", "source"), "IN_CATEGORY": ("procurements", "source"),
    "USES_MODE": ("procurements", "source"), "NOTICE_FOR": ("bid_notices", "source"),
    "SUBMITTED": ("bids", "target"), "FOR_PROCUREMENT": ("bids", "source"), "UNDER_NOTICE": ("bids", "source"),
    "RESOLVES": ("bac_resolutions", "source"), "RECOMMENDS": ("bac_resolutions", "source"),
    "AWARDS": ("award_notices", "source"), "AWARDED_TO": ("award_notices", "source"),
    "BASED_ON_BID": ("award_notices", "source"), "BASED_ON_RESOLUTION": ("award_notices", "source"),
    "UNDER_CONTRACT": ("award_notices", "source"), "CERTIFIED_BY": ("award_notices", "source"),
    "REPRESENTED_BY": ("supplier_representatives", "both"),
}
# Consistency with the sync definitions (graph_sync.REL_SOURCES: table, row id column, from column, to column).
for _t, (_table, _side) in REL_SOURCE_RECORD.items():
    _st, _id, _from, _to = REL_SOURCES[_t][:4]
    _expected = "both" if _t == "REPRESENTED_BY" else ("source" if _id == _from else "target" if _id == _to else None)
    if _st != _table or _side != _expected:
        raise RuntimeError("graph_store: relationship ID mapping does not match graph_sync.REL_SOURCES")
if set(REL_SOURCE_RECORD) != set(REL_TYPES):
    raise RuntimeError("graph_store: relationship ID mapping must cover every relationship type")


class _Stop(Exception):
    def __init__(self, status, body):
        super().__init__(body.get("message"))
        self.status, self.body = status, body


def _run(tx, text, params):
    """Run one read statement in a read transaction and return plain dicts. (Replaced in simulated tests.)"""
    return [record.data() for record in tx.run(text, params)]


def _read_all(tx):
    params = {"owner": OWNER, "infoKey": INFO_KEY}
    return _run(tx, READ_INFO, params), _run(tx, READ_NODES, params), _run(tx, READ_RELS, params)


def _plain(value):
    """Neo4j value -> JSON value. Temporal values become ISO strings."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if hasattr(value, "iso_format"):
        return value.iso_format()
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _error(status, state, message, **extra):
    body = {"status": "error", "source": "neo4j", "neo4j": state, "message": message}
    body.update(extra)
    return _Stop(status, body)


def _sync_info(rows):
    if not rows:
        return None
    row = rows[0]
    summary = {}
    if isinstance(row.get("lastSummary"), str):
        try:
            summary = json.loads(row["lastSummary"])
        except ValueError:
            summary = {}
    if not isinstance(summary, dict):
        summary = {}
    totals = summary.get("totals") if isinstance(summary.get("totals"), dict) else None
    return {
        "lastRunId": _plain(row.get("lastRunId")),
        "lastSyncedAt": _plain(row.get("lastSyncedAt")),
        "verificationOk": summary.get("verificationOk") if isinstance(summary.get("verificationOk"), bool) else None,
        "totals": totals,
    }


def _relationship_id(rtype, a, b):
    """"<TYPE>:<source table>:<primary key of the source record>", or None when it cannot be established
    (unknown type, end node of an unexpected label, or end node without a stable key)."""
    if rtype not in REL_SOURCE_RECORD:
        return None
    from_label, to_label, _fks = REL_TYPES[rtype]
    if a["label"] != from_label or b["label"] != to_label or not a["stableId"] or not b["stableId"]:
        return None
    table, side = REL_SOURCE_RECORD[rtype]
    key = {"source": str(a["key"]), "target": str(b["key"]), "both": f"{a['key']},{b['key']}"}[side]
    return f"{rtype}:{table}:{key}"


def build_graph(info_rows, node_rows, rel_rows):
    """Turn the three read results into the API body (pure function; no I/O)."""
    nodes, by_eid, used_ids = [], {}, set()
    unmapped = 0
    for i, row in enumerate(sorted(node_rows, key=lambda r: (sorted(r.get("labels") or []), str(r.get("eid"))))):
        labels = [l for l in (row.get("labels") or []) if l != OWNER_LABEL]
        props = {k: _plain(v) for k, v in (row.get("props") or {}).items() if k not in MARKER_PROPS}
        label = next((l for l in labels if l in NODE_KEYS), labels[0] if labels else None)
        key_prop = NODE_KEYS.get(label)
        key = props.get(key_prop) if key_prop else None
        stable = key is not None and isinstance(key, (int, str)) and not isinstance(key, bool)
        node_id = f"{label}:{key}" if stable else f"unmapped:{i + 1}"
        if node_id in used_ids:                        # two nodes claiming one key: keep both, flag the second
            stable, node_id = False, f"unmapped:{i + 1}"
        used_ids.add(node_id)
        if not stable or label not in NODE_KEYS:
            unmapped += 1
        node = {"id": node_id, "label": label or "(no label)", "labels": labels, "known": label in NODE_KEYS,
                "stableId": stable, "key": key if stable else None, "properties": props}
        nodes.append(node)
        by_eid[row.get("eid")] = node
    nodes.sort(key=lambda n: (n["label"], str(type(n["key"])), n["key"] if n["key"] is not None else "", n["id"]))

    rels, unknown_types, seen = [], set(), set()
    for row in rel_rows:
        a, b = by_eid.get(row.get("srcEid")), by_eid.get(row.get("dstEid"))
        if a is None or b is None:                     # end node not owned / not returned: never shown
            continue
        rtype = row.get("relType")
        known = rtype in REL_TYPES
        if not known:
            unknown_types.add(rtype)
        rels.append({"id": None, "type": rtype, "known": known, "stableId": False, "source": a["id"], "target": b["id"],
                     "_ids": _relationship_id(rtype, a, b),
                     "properties": {k: _plain(v) for k, v in (row.get("props") or {}).items() if k not in MARKER_PROPS}})
    rels.sort(key=lambda r: (r["type"] or "", r["source"], r["target"], r["_ids"] or ""))
    duplicates = 0
    for i, r in enumerate(rels):
        rid = r.pop("_ids")
        if rid is not None and rid in seen:            # cannot happen for data written by graph_sync.py; flagged if it does
            duplicates += 1
            rid = None
        if rid is None:
            r["id"] = f"unmapped-rel:{i + 1}"
        else:
            r["id"], r["stableId"] = rid, True
            seen.add(rid)

    sources = sorted({n["properties"]["dataSource"] for n in nodes if isinstance(n["properties"].get("dataSource"), str)})
    return {
        "status": "success",
        "source": "neo4j",
        "snapshot": True,
        "dataLabel": DATA_LABEL,
        "notice": NOTICE,
        "dataSources": sources,
        "sync": _sync_info(info_rows),
        "schema": {"nodeLabels": list(NODE_KEYS), "relationshipTypes": list(REL_TYPES)},
        "counts": {"nodes": len(nodes), "relationships": len(rels), "unmappedNodes": unmapped,
                   "unknownRelationshipTypes": sorted(t for t in unknown_types if t is not None),
                   "unmappedRelationships": sum(1 for r in rels if not r["stableId"]),
                   "duplicateRelationshipIds": duplicates},
        "nodes": nodes,
        "relationships": rels,
    }


def read_graph():
    """Read the owned Neo4j projection. Returns (http status, body). Never raises, never writes."""
    try:
        if neo4j_db.neo4j is None:
            raise _error(503, "driver_missing", "The Neo4j Python driver is not installed on the server.")
        try:
            settings = neo4j_db.read_settings()
        except neo4j_db.Neo4jSettingsError as exc:
            raise _error(503, "not_configured", str(exc))
        driver = neo4j_db.get_driver(settings)
        with driver.session(database=settings.database, default_access_mode=neo4j_db.neo4j.READ_ACCESS) as session:
            info_rows, node_rows, rel_rows = session.execute_read(_read_all)
        if not info_rows and not node_rows:
            raise _error(404, "not_synced", "The Neo4j graph has not been synchronized yet. A Super Admin can run the "
                                            "synchronization; until then there is nothing to show.")
        body = build_graph(info_rows, node_rows, rel_rows)
        unexpected = sorted(set(body["dataSources"]) - set(GRAPH_SOURCES))
        if unexpected:
            raise _error(409, "unexpected_data", "The Neo4j graph contains records from a data source that is not "
                                                 "approved for this view. Nothing is shown.", dataSources=unexpected)
        body["database"] = settings.database
        return 200, body
    except _Stop as stop:
        return stop.status, stop.body
    except Exception as exc:
        code = neo4j_db._neo4j_code(exc)
        if neo4j_db.neo4j is not None and isinstance(exc, neo4j_db.AuthError):
            neo4j_db.close_driver()
            state, msg = "auth_failed", "Neo4j rejected the configured user name or password."
        elif neo4j_db.neo4j is not None and isinstance(exc, neo4j_db.ServiceUnavailable):
            state, msg = "unreachable", "Neo4j is not reachable. Check that the database is started."
        elif code.endswith("DatabaseNotFound"):
            state, msg = "database_not_found", "The database named in NEO4J_DATABASE does not exist."
        else:
            state, msg = "error", "The Neo4j graph could not be read. See the Flask terminal for the error type."
        _log.warning("Neo4j graph read failed (%s%s)", type(exc).__name__, (", " + code) if code else "")
        return 503, {"status": "error", "source": "neo4j", "neo4j": state, "message": msg}
