"""
backend/graph_api.py - Sample City Knowledge Graph data (READ-ONLY).

    GET /api/graph/sample        signed-in BAC staff (Secretariat, BAC Member, Super Admin)
    GET /api/graph/neo4j/health  Super Admin only: Neo4j connectivity check (see neo4j_db.py).
                                 Reads no procurement data; the graph itself still comes from MariaDB.
    POST /api/graph/neo4j/sync   Super Admin only: copy the approved sample records from MariaDB into
                                 Neo4j (see graph_sync.py). MariaDB is only read. No request body is
                                 used, so the browser cannot change what is synchronized.

Returns the records the Sample City Graph is built from: procurements, procuring entities,
suppliers and their representatives, bids, BAC resolutions and award notices.

Scope (approved):
  * Only records with data_source = 'sample' (fictional demonstration data). The source list
    is fixed here on the server (GRAPH_SOURCES); the browser cannot ask for anything else.
    'encoded' test drafts and 'philgeps' reference rows are never returned.
  * Archived (soft-deleted) procurements are excluded.
  * SELECT statements only. Nothing is written, no schema object is created or changed.
  * Not public: bids and resolutions are internal BAC information.
  * No connection to any other system (no import, no scraping, no external calls).

Future authorized City records: add the source to GRAPH_SOURCES and its label to
SOURCE_LABELS after approval. Nothing else in this module or the page assumes "sample".
"""

from datetime import date, datetime
from decimal import Decimal

from flask import Blueprint, current_app, g, jsonify

import neo4j_db
from auth import require_role
from db import get_db_connection

graph_bp = Blueprint("graph", __name__)

STAFF_ROLES = ("secretariat", "member", "admin")

GRAPH_SOURCES = ("sample",)                          # approved: sample records only
SOURCE_LABELS = {"sample": "Sample Data"}            # shown on every sample node, statistic and export
SOURCE_NOTICES = {
    "sample": ("Fictional sample procurement records for demonstration only. They are not "
               "Santa Rosa City procurement records and not PhilGEPS reference data."),
}
_PERMITTED = {"sample", "encoded"}                   # 'philgeps' reference rows are never part of the City graph

if not GRAPH_SOURCES or set(GRAPH_SOURCES) - _PERMITTED or set(GRAPH_SOURCES) - set(SOURCE_LABELS):
    raise RuntimeError("graph_api.GRAPH_SOURCES must list approved City sources, each with a label")

_IN = "(" + ", ".join(["%s"] * len(GRAPH_SOURCES)) + ")"   # placeholders only; values are passed as parameters

PROCUREMENTS_SQL = f"""
SELECT p.procurement_id, p.reference_no, p.title, p.end_user_unit, p.procurement_class, p.abc,
       p.status, p.fiscal_year, p.target_timeline, p.data_source,
       pe.entity_id, pe.name AS entity_name, pe.city AS entity_city, pe.province AS entity_province,
       c.name AS category_name, m.name AS mode_name
FROM procurements p
JOIN procuring_entities pe ON pe.entity_id = p.procuring_entity_id
JOIN procurement_modes m   ON m.mode_id    = p.mode_id
LEFT JOIN categories c     ON c.category_id = p.category_id
WHERE p.data_source IN {_IN}
  AND p.archived_at IS NULL
ORDER BY p.procurement_id
"""

BIDS_SQL = f"""
SELECT b.bid_id, b.procurement_id, b.supplier_id, b.bid_amount, b.rank_no
FROM bids b
JOIN procurements p ON p.procurement_id = b.procurement_id
JOIN suppliers s    ON s.supplier_id    = b.supplier_id
WHERE p.data_source IN {_IN}
  AND s.data_source IN {_IN}
  AND p.archived_at IS NULL
ORDER BY b.procurement_id, b.rank_no IS NULL, b.rank_no, b.bid_id
"""

RESOLUTIONS_SQL = f"""
SELECT r.resolution_id, r.procurement_id, r.resolution_no, r.recommendation, r.recommended_bid_id,
       r.post_qualification_done, r.resolution_date
FROM bac_resolutions r
JOIN procurements p ON p.procurement_id = r.procurement_id
WHERE p.data_source IN {_IN}
  AND p.archived_at IS NULL
ORDER BY r.procurement_id, r.resolution_id
"""

# Award notices come from the existing view v_award_graph_records (used as it is).
# The view has no archived_at and no supplier data_source, so procurements and suppliers
# are joined here to apply both filters.
AWARDS_SQL = f"""
SELECT v.award_id, v.procurement_id, v.supplier_id, v.bid_id, v.notice_no, v.award_date,
       v.published_date, v.awarded_amount, v.award_basis, v.lot_no,
       v.contract_id, v.contract_no, v.certifier_id, v.certifier_name, v.certifier_role
FROM v_award_graph_records v
JOIN procurements p ON p.procurement_id = v.procurement_id
JOIN suppliers s    ON s.supplier_id    = v.supplier_id
WHERE v.data_source IN {_IN}
  AND s.data_source IN {_IN}
  AND p.archived_at IS NULL
ORDER BY v.procurement_id, v.award_id
"""

SUPPLIERS_SQL = f"""
SELECT s.supplier_id, s.name, s.address, s.city, s.province, s.data_source
FROM suppliers s
WHERE s.data_source IN {_IN}
ORDER BY s.supplier_id
"""

REPRESENTATIVES_SQL = f"""
SELECT r.supplier_id, r.position, pr.person_id, pr.full_name
FROM supplier_representatives r
JOIN suppliers s ON s.supplier_id = r.supplier_id
JOIN persons pr  ON pr.person_id  = r.person_id
WHERE s.data_source IN {_IN}
ORDER BY r.supplier_id, pr.person_id
"""


def _num(value):
    if value is None:
        return None
    return float(value) if isinstance(value, Decimal) else float(value)


def _day(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _rows(cur, sql, repeat):
    cur.execute(sql, tuple(GRAPH_SOURCES) * repeat)
    return cur.fetchall()


def fetch_sample_graph(conn):
    """Run the read-only queries and return the graph records (plain dicts, JSON-ready)."""
    cur = conn.cursor(dictionary=True, buffered=True)
    try:
        procs = _rows(cur, PROCUREMENTS_SQL, 1)
        bids = _rows(cur, BIDS_SQL, 2)
        resolutions = _rows(cur, RESOLUTIONS_SQL, 1)
        awards = _rows(cur, AWARDS_SQL, 2)
        suppliers = _rows(cur, SUPPLIERS_SQL, 1)
        reps = _rows(cur, REPRESENTATIVES_SQL, 1)
    finally:
        cur.close()

    proc_ids = {p["procurement_id"] for p in procs}
    # Only suppliers that take part in a returned bid or award are graph nodes.
    used = {b["supplier_id"] for b in bids} | {a["supplier_id"] for a in awards}
    reps_by_supplier = {}
    for r in reps:
        reps_by_supplier.setdefault(r["supplier_id"], []).append(
            {"personId": r["person_id"], "name": r["full_name"], "position": r["position"]})

    out = {
        "procurements": [{
            "procurementId": p["procurement_id"], "id": p["reference_no"], "title": p["title"],
            "unit": p["end_user_unit"], "category": p["procurement_class"],
            "commodityCategory": p["category_name"], "mode": p["mode_name"], "abc": _num(p["abc"]),
            "status": p["status"], "fiscalYear": p["fiscal_year"], "timeline": p["target_timeline"],
            "dataSource": p["data_source"],
            "entity": {"id": p["entity_id"], "name": p["entity_name"],
                       "city": p["entity_city"], "province": p["entity_province"]},
        } for p in procs],
        "suppliers": [{
            "supplierId": s["supplier_id"], "name": s["name"], "address": s["address"],
            "city": s["city"], "province": s["province"], "dataSource": s["data_source"],
            "representatives": reps_by_supplier.get(s["supplier_id"], []),
        } for s in suppliers if s["supplier_id"] in used],
        "bids": [{
            "bidId": b["bid_id"], "procurementId": b["procurement_id"], "supplierId": b["supplier_id"],
            "amount": _num(b["bid_amount"]), "rank": b["rank_no"],
        } for b in bids if b["procurement_id"] in proc_ids],
        "resolutions": [{
            "resolutionId": r["resolution_id"], "procurementId": r["procurement_id"],
            "resolutionNo": r["resolution_no"], "recommendation": r["recommendation"],
            "recommendedBidId": r["recommended_bid_id"],
            "postQualificationDone": bool(r["post_qualification_done"]), "date": _day(r["resolution_date"]),
        } for r in resolutions if r["procurement_id"] in proc_ids],
        "awards": [{
            "awardId": a["award_id"], "procurementId": a["procurement_id"], "supplierId": a["supplier_id"],
            "bidId": a["bid_id"], "noticeNo": a["notice_no"], "awardDate": _day(a["award_date"]),
            "publishedDate": _day(a["published_date"]), "amount": _num(a["awarded_amount"]),
            "basis": a["award_basis"], "lotNo": a["lot_no"],
            "contract": ({"id": a["contract_id"], "no": a["contract_no"]} if a["contract_id"] is not None else None),
            "certifier": ({"id": a["certifier_id"], "name": a["certifier_name"], "role": a["certifier_role"]}
                          if a["certifier_id"] is not None else None),
        } for a in awards if a["procurement_id"] in proc_ids],
    }
    out["counts"] = {k: len(v) for k, v in out.items()}
    return out


@graph_bp.route("/api/graph/sample", methods=["GET"])
@require_role(*STAFF_ROLES)
def get_sample_graph():
    """Sample City Graph records (read-only)."""
    conn = None
    try:
        conn = get_db_connection()
        data = fetch_sample_graph(conn)
    except Exception:
        current_app.logger.exception("Failed to load the sample graph")
        return jsonify({"status": "error", "message": "Could not load the sample graph."}), 500
    finally:
        if conn is not None:
            conn.close()
    source = GRAPH_SOURCES[0] if len(GRAPH_SOURCES) == 1 else "mixed"
    return jsonify({
        "status": "success",
        "dataSources": list(GRAPH_SOURCES),
        "dataLabel": " + ".join(SOURCE_LABELS[s] for s in GRAPH_SOURCES),
        "notice": " ".join(SOURCE_NOTICES.get(s, "") for s in GRAPH_SOURCES).strip(),
        "source": source,
        **data,
    })

@graph_bp.route("/api/graph/neo4j", methods=["GET"])
@require_role(*STAFF_ROLES)
def get_neo4j_graph():
    """Read the synchronized Neo4j graph (staff only)."""
    import graph_store

    status_code, body = graph_store.read_graph()
    response = jsonify(body)
    response.status_code = status_code
    response.headers["Cache-Control"] = "no-store"
    return response


@graph_bp.route("/api/graph/neo4j/health", methods=["GET"])
@require_role("admin")
def get_neo4j_health():
    """Neo4j connectivity check (Super Admin only). 200 when connected, otherwise 503.
    The response never contains the Neo4j URI, user name or password."""
    status_code, body = neo4j_db.check_health()
    response = jsonify(body)
    response.status_code = status_code
    response.headers["Cache-Control"] = "no-store"
    return response

@graph_bp.route("/api/graph/neo4j/verify", methods=["GET"])
@require_role("admin")
def get_neo4j_verify():
    """Read-only comparison of MariaDB and Neo4j (Super Admin only)."""
    import graph_sync

    status_code, body = graph_sync.run_verify()
    response = jsonify(body)
    response.status_code = status_code
    response.headers["Cache-Control"] = "no-store"
    return response


@graph_bp.route("/api/graph/neo4j/sync", methods=["POST"])
@require_role("admin")
def post_neo4j_sync():
    """Synchronize MariaDB -> Neo4j (Super Admin only). 200 with a summary when Neo4j matches MariaDB;
    409 when a safety rule refused; 503 when Neo4j is unavailable; 500 otherwise. Never exposes
    credentials or raw database errors. Requires the X-Requested-With: fetch header (auth.py)."""
    import graph_sync                    # imported here: graph_sync imports GRAPH_SOURCES from this module
    status_code, body = graph_sync.run_sync()
    totals = body.get("totals") or {}
    current_app.logger.info(
        "Neo4j graph sync by user_id %s: HTTP %s, stage %s, nodes created %s, relationships created %s",
        g.user.get("user_id"), status_code, body.get("stage", "done"),
        totals.get("nodesCreated", "-"), totals.get("relationshipsCreated", "-"))
    response = jsonify(body)
    response.status_code = status_code
    response.headers["Cache-Control"] = "no-store"
    return response
