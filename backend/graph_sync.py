"""
backend/graph_sync.py - Project the approved sample graph records from MariaDB into Neo4j.

MariaDB is the source of truth. Neo4j receives a projection (a copy) that this module
fully owns and can rebuild at any time. Nothing here writes to MariaDB.

Run it
  * Super Admin, from the page or a script:  POST /api/graph/neo4j/sync   (see graph_api.py)
  * Command line (from backend/):
        python graph_sync.py              synchronize, print the summary (JSON)
        python graph_sync.py --verify     read-only: compare Neo4j with MariaDB, change nothing
        python graph_sync.py --wipe       DRY RUN: show what would be removed from Neo4j
        python graph_sync.py --wipe --yes remove the projection (owned data and owned constraints only)

Scope (same as GET /api/graph/sample, from graph_api.GRAPH_SOURCES):
  procurements with data_source IN GRAPH_SOURCES and archived_at IS NULL; their bid notices,
  BAC resolutions; bids and award notices whose supplier also has data_source IN GRAPH_SOURCES;
  the suppliers, procuring entities, categories, procurement modes, contracts and persons those
  rows reference. Never 'philgeps' reference rows, never user accounts, never TIN.

MariaDB safety
  * The connection must be to procurement_db_frontend_test (checked with SELECT DATABASE()).
  * All reads run in ONE "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY" transaction
    (consistent snapshot; MariaDB itself rejects any write) and end with ROLLBACK.
  * Every relationship below is projected only if the foreign-key constraint that grounds it exists in
    the live schema as ONE constraint with the expected ordered column mapping (checked from
    information_schema.KEY_COLUMN_USAGE grouped by CONSTRAINT_NAME and ordered by ORDINAL_POSITION;
    four of them are composite). Any missing, split or differently ordered constraint stops the sync
    before the data is read and before anything is written to Neo4j.

Neo4j ownership (nothing outside the projection is ever changed)
  * Every node carries the label BacPmsGraph and syncOwner = OWNER; every relationship carries
    syncOwner = OWNER. The sync refuses to start if Neo4j holds a node with one of its labels, or a
    relationship with one of its types, that is not owned - MERGE would otherwise adopt it.
  * One write transaction: MERGE nodes and relationships by primary key, stamp syncRunId, then
    remove owned items that MariaDB no longer has (for example an archived procurement). If such an
    item still has a relationship this module did not create, the whole transaction is rolled back.
  * Uniqueness constraints are named bac_pms_*; the names this module actually created are recorded
    on the (:BacPmsSyncInfo) node. --wipe drops only those, only if the definition still matches and
    the label has no nodes left. When ownership is uncertain, --wipe refuses.

Cypher: values are always query parameters. Labels, relationship types and property keys cannot be
parameters in Cypher; they come only from the fixed tables below and are validated at import.
"""

import json
import logging
import re
import sys
import threading
import time
import uuid
from collections import namedtuple
from datetime import date, datetime, timezone
from decimal import Decimal

import neo4j_db
from db import get_db_connection
from graph_api import GRAPH_SOURCES

_log = logging.getLogger(__name__)

EXPECTED_DATABASE = "procurement_db_frontend_test"
OWNER = "santa-rosa-bac-pms"
OWNER_LABEL = "BacPmsGraph"
INFO_LABEL = "BacPmsSyncInfo"
INFO_KEY = "bac-pms-graph"
WRITE_TIMEOUT_SECONDS = 120
CHUNK = 500                                  # rows per UNWIND batch and per SQL IN (...) list

# label -> key property (the MariaDB primary key)
NODE_KEYS = {
    "Procurement": "procurementId",
    "ProcuringEntity": "entityId",
    "Category": "categoryId",
    "ProcurementMode": "modeId",
    "BidNotice": "noticeId",
    "Supplier": "supplierId",
    "Bid": "bidId",
    "BacResolution": "resolutionId",
    "AwardNotice": "awardId",
    "Contract": "contractId",
    "Person": "personId",
}

# Foreign-key constraints that ground the relationships, exactly as defined in the MariaDB schema
# (schema_proposal_v4.sql). Columns are ORDERED: position i of `columns` references position i of
# `ref_columns`. A composite constraint must exist as ONE constraint with exactly these columns in this
# order; matching individual column pairs is not enough. Constraint names are reported; the match is
# made on the table, referenced table and ordered column mapping.
ForeignKey = namedtuple("ForeignKey", "name table columns ref_table ref_columns")
FOREIGN_KEYS = {fk.name: fk for fk in (
    ForeignKey("fk_proc_entity", "procurements", ("procuring_entity_id",), "procuring_entities", ("entity_id",)),
    ForeignKey("fk_proc_category", "procurements", ("category_id",), "categories", ("category_id",)),
    ForeignKey("fk_proc_mode", "procurements", ("mode_id",), "procurement_modes", ("mode_id",)),
    ForeignKey("fk_notices_procurement", "bid_notices", ("procurement_id",), "procurements", ("procurement_id",)),
    ForeignKey("fk_bids_procurement", "bids", ("procurement_id",), "procurements", ("procurement_id",)),
    ForeignKey("fk_bids_supplier", "bids", ("supplier_id",), "suppliers", ("supplier_id",)),
    ForeignKey("fk_bids_notice_same_proc", "bids", ("notice_id", "procurement_id"),
               "bid_notices", ("notice_id", "procurement_id")),
    ForeignKey("fk_res_procurement", "bac_resolutions", ("procurement_id",), "procurements", ("procurement_id",)),
    ForeignKey("fk_res_bid_same_proc", "bac_resolutions", ("recommended_bid_id", "procurement_id"),
               "bids", ("bid_id", "procurement_id")),
    ForeignKey("fk_awards_procurement", "award_notices", ("procurement_id",), "procurements", ("procurement_id",)),
    ForeignKey("fk_awards_supplier", "award_notices", ("supplier_id",), "suppliers", ("supplier_id",)),
    ForeignKey("fk_awards_contract", "award_notices", ("contract_id",), "contracts", ("contract_id",)),
    ForeignKey("fk_awards_bid_same_proc_supplier", "award_notices", ("bid_id", "procurement_id", "supplier_id"),
               "bids", ("bid_id", "procurement_id", "supplier_id")),
    ForeignKey("fk_awards_resolution_same_proc", "award_notices", ("resolution_id", "procurement_id"),
               "bac_resolutions", ("resolution_id", "procurement_id")),
    ForeignKey("fk_awards_certifier", "award_notices", ("certified_by_person_id",), "persons", ("person_id",)),
    ForeignKey("fk_reps_supplier", "supplier_representatives", ("supplier_id",), "suppliers", ("supplier_id",)),
    ForeignKey("fk_reps_person", "supplier_representatives", ("person_id",), "persons", ("person_id",)),
)}

# type -> (from label, to label, names of the foreign-key constraints in FOREIGN_KEYS that ground it)
REL_TYPES = {
    "PROCURED_BY":         ("Procurement", "ProcuringEntity", ["fk_proc_entity"]),
    "IN_CATEGORY":         ("Procurement", "Category",        ["fk_proc_category"]),
    "USES_MODE":           ("Procurement", "ProcurementMode", ["fk_proc_mode"]),
    "NOTICE_FOR":          ("BidNotice", "Procurement",       ["fk_notices_procurement"]),
    "SUBMITTED":           ("Supplier", "Bid",                ["fk_bids_supplier"]),
    "FOR_PROCUREMENT":     ("Bid", "Procurement",             ["fk_bids_procurement"]),
    "UNDER_NOTICE":        ("Bid", "BidNotice",               ["fk_bids_notice_same_proc"]),
    "RESOLVES":            ("BacResolution", "Procurement",   ["fk_res_procurement"]),
    "RECOMMENDS":          ("BacResolution", "Bid",           ["fk_res_bid_same_proc"]),
    "AWARDS":              ("AwardNotice", "Procurement",     ["fk_awards_procurement"]),
    "AWARDED_TO":          ("AwardNotice", "Supplier",        ["fk_awards_supplier"]),
    "BASED_ON_BID":        ("AwardNotice", "Bid",             ["fk_awards_bid_same_proc_supplier"]),
    "BASED_ON_RESOLUTION": ("AwardNotice", "BacResolution",   ["fk_awards_resolution_same_proc"]),
    "UNDER_CONTRACT":      ("AwardNotice", "Contract",        ["fk_awards_contract"]),
    "CERTIFIED_BY":        ("AwardNotice", "Person",          ["fk_awards_certifier"]),
    "REPRESENTED_BY":      ("Supplier", "Person",             ["fk_reps_supplier", "fk_reps_person"]),
}

_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _snake(name):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


# constraint name -> (label, property)
CONSTRAINTS = {f"bac_pms_{_snake(label)}_{_snake(key)}_unique": (label, key) for label, key in NODE_KEYS.items()}
CONSTRAINTS["bac_pms_sync_info_key_unique"] = (INFO_LABEL, "syncKey")

for _name in [OWNER_LABEL, INFO_LABEL, *NODE_KEYS, *NODE_KEYS.values(), *REL_TYPES, *CONSTRAINTS]:
    if not _IDENT.match(_name):
        raise RuntimeError("graph_sync: invalid identifier in the fixed graph tables")
for _t, (_a, _b, _fks) in REL_TYPES.items():
    if _a not in NODE_KEYS or _b not in NODE_KEYS or not _fks or any(f not in FOREIGN_KEYS for f in _fks):
        raise RuntimeError("graph_sync: relationship table refers to an unknown label or foreign key")
for _fk in FOREIGN_KEYS.values():
    if not _fk.columns or len(_fk.columns) != len(_fk.ref_columns):
        raise RuntimeError("graph_sync: foreign key definition has mismatched column lists")
DATA_LABELS = list(NODE_KEYS)
ALL_LABELS = DATA_LABELS + [OWNER_LABEL, INFO_LABEL]


class SyncRefused(Exception):
    """A safety rule stopped the operation before anything unsafe happened (HTTP 409)."""
    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}


class SyncDataError(Exception):
    """A MariaDB value could not be converted safely. The message names table/column/id, never the value."""


# ---------------------------------------------------------------- MariaDB (read only)

_S = "(" + ", ".join(["%s"] * len(GRAPH_SOURCES)) + ")"
_SCOPE = f"p.data_source IN {_S} AND p.archived_at IS NULL"

# One row per column of every foreign-key constraint in this database; ORDINAL_POSITION is the
# column's position inside its constraint, so rows can be regrouped into whole, ordered constraints.
SQL_FOREIGN_KEYS = """
SELECT CONSTRAINT_NAME, TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION,
       REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
FROM information_schema.KEY_COLUMN_USAGE
WHERE TABLE_SCHEMA = DATABASE()
  AND REFERENCED_TABLE_SCHEMA = DATABASE()
  AND REFERENCED_TABLE_NAME IS NOT NULL
ORDER BY TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION
"""

SQL_PROCUREMENTS = f"""
SELECT p.procurement_id, p.reference_no, p.title, p.procuring_entity_id, p.end_user_unit, p.procurement_class,
       p.category_id, p.mode_id, p.abc, p.control_no, p.target_timeline, p.fiscal_year, p.status, p.data_source
FROM procurements p
WHERE {_SCOPE}
ORDER BY p.procurement_id
"""

SQL_BID_NOTICES = f"""
SELECT n.notice_id, n.procurement_id, n.notice_type, n.philgeps_ref, n.published_date, n.submission_deadline,
       n.bids_received
FROM bid_notices n
JOIN procurements p ON p.procurement_id = n.procurement_id
WHERE {_SCOPE}
ORDER BY n.notice_id
"""

SQL_BIDS = f"""
SELECT b.bid_id, b.procurement_id, b.notice_id, b.supplier_id, b.bid_amount, b.rank_no
FROM bids b
JOIN procurements p ON p.procurement_id = b.procurement_id
JOIN suppliers s    ON s.supplier_id    = b.supplier_id
WHERE {_SCOPE} AND s.data_source IN {_S}
ORDER BY b.bid_id
"""

SQL_RESOLUTIONS = f"""
SELECT r.resolution_id, r.procurement_id, r.resolution_no, r.recommendation, r.recommended_bid_id,
       r.post_qualification_done, r.resolution_date
FROM bac_resolutions r
JOIN procurements p ON p.procurement_id = r.procurement_id
WHERE {_SCOPE}
ORDER BY r.resolution_id
"""

SQL_AWARDS = f"""
SELECT a.award_id, a.procurement_id, a.supplier_id, a.contract_id, a.bid_id, a.resolution_id, a.notice_no,
       a.bid_reference_no, a.award_date, a.published_date, a.awarded_amount, a.award_basis, a.mode_as_recorded,
       a.lot_no, a.lot_description, a.lot_quantity, a.certified_by_person_id, a.certifier_role
FROM award_notices a
JOIN procurements p ON p.procurement_id = a.procurement_id
JOIN suppliers s    ON s.supplier_id    = a.supplier_id
WHERE {_SCOPE} AND s.data_source IN {_S}
ORDER BY a.award_id
"""

# Rows referenced by the rows above, fetched by primary key: "... IN (<placeholders>)" is appended.
SQL_BY_IDS = {
    "suppliers": (f"SELECT supplier_id, name, address, city, province, data_source FROM suppliers "
                  f"WHERE data_source IN {_S} AND supplier_id IN ", True),
    "procuring_entities": ("SELECT entity_id, name, city, province FROM procuring_entities WHERE entity_id IN ", False),
    "categories": ("SELECT category_id, name FROM categories WHERE category_id IN ", False),
    "procurement_modes": ("SELECT mode_id, name, is_competitive FROM procurement_modes WHERE mode_id IN ", False),
    "contracts": ("SELECT contract_id, contract_no, contract_date FROM contracts WHERE contract_id IN ", False),
    "supplier_representatives": ("SELECT supplier_id, person_id, position FROM supplier_representatives "
                                 "WHERE supplier_id IN ", False),
    "persons": ("SELECT person_id, full_name FROM persons WHERE person_id IN ", False),
}


def _fetch(cur, sql, params):
    cur.execute(sql, tuple(params))
    return cur.fetchall()


def _fetch_by_ids(cur, table, ids):
    sql, scoped = SQL_BY_IDS[table]
    ids = sorted(i for i in set(ids) if i is not None)
    rows = []
    for start in range(0, len(ids), CHUNK):
        part = ids[start:start + CHUNK]
        params = (tuple(GRAPH_SOURCES) if scoped else ()) + tuple(part)
        rows.extend(_fetch(cur, sql + "(" + ", ".join(["%s"] * len(part)) + ")", params))
    return rows


def required_foreign_keys():
    """The ForeignKey definitions the 16 relationship types rely on (17 constraints), sorted by name."""
    return [FOREIGN_KEYS[n] for n in sorted({f for _a, _b, fks in REL_TYPES.values() for f in fks})]


def _describe(table, columns, ref_table, ref_columns):
    return f"{table} ({', '.join(columns)}) -> {ref_table} ({', '.join(ref_columns)})"


def group_foreign_keys(rows):
    """KEY_COLUMN_USAGE rows -> list of whole constraints, columns ordered by ORDINAL_POSITION.
    A constraint whose positions are not exactly 1..n, or that points at more than one table,
    is kept but marked malformed (it can never satisfy a requirement)."""
    groups = {}
    for r in rows:
        key = (r["TABLE_NAME"], r["CONSTRAINT_NAME"])
        groups.setdefault(key, []).append(r)
    out = []
    for (table, name), parts in sorted(groups.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        try:
            parts = sorted(parts, key=lambda r: int(r["ORDINAL_POSITION"]))
            positions = [int(r["ORDINAL_POSITION"]) for r in parts]
        except (TypeError, ValueError):
            positions = []
        ref_tables = {r["REFERENCED_TABLE_NAME"] for r in parts}
        out.append({
            "name": name, "table": table,
            "ref_table": next(iter(ref_tables)) if len(ref_tables) == 1 else None,
            "columns": tuple(r["COLUMN_NAME"] for r in parts),
            "ref_columns": tuple(r["REFERENCED_COLUMN_NAME"] for r in parts),
            "wellFormed": positions == list(range(1, len(parts) + 1)) and len(ref_tables) == 1,
        })
    return out


def check_foreign_keys(rows):
    """Compare the live constraints with FOREIGN_KEYS. Returns (matched {expected name: actual name}, problems).
    Each required constraint must exist as ONE well-formed constraint with the same table, referenced
    table and ordered column mapping. Problems explain what was found instead."""
    groups = group_foreign_keys(rows)
    matched, problems = {}, []
    for fk in required_foreign_keys():
        hit = [g for g in groups if g["wellFormed"] and g["table"] == fk.table and g["ref_table"] == fk.ref_table
               and g["columns"] == fk.columns and g["ref_columns"] == fk.ref_columns]
        if hit:
            matched[fk.name] = hit[0]["name"]
            continue
        wanted = set(zip(fk.columns, fk.ref_columns))
        # constraints on the same table that point at the same table, or carry the expected name
        related = [g for g in groups if g["table"] == fk.table and
                   (g["ref_table"] == fk.ref_table or g["name"] == fk.name)]
        if not related:
            reason = "missing"
        elif any(set(zip(g["columns"], g["ref_columns"])) == wanted and g["ref_table"] == fk.ref_table for g in related):
            reason = "columns in a different order" if all(g["wellFormed"] for g in related) else "malformed constraint metadata"
        elif len(related) > 1 and wanted <= set().union(*(set(zip(g["columns"], g["ref_columns"])) for g in related)):
            reason = "split across several constraints"
        else:
            reason = "different columns or referenced table"
        problems.append({
            "constraint": fk.name,
            "expected": _describe(fk.table, fk.columns, fk.ref_table, fk.ref_columns),
            "problem": reason,
            "found": [g["name"] + ": " + _describe(g["table"], g["columns"], g["ref_table"] or "?", g["ref_columns"])
                      for g in related],
        })
    return matched, problems


def read_snapshot(conn):
    """Read everything the projection needs in one consistent, read-only MariaDB transaction."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT DATABASE()")
        database = cur.fetchone()[0]
    finally:
        cur.close()
    if database != EXPECTED_DATABASE:
        raise SyncRefused(f"MariaDB is not {EXPECTED_DATABASE}. Check DB_CONFIG in backend/config.py.")

    conn.rollback()                           # end any implicit transaction before starting the snapshot
    conn.start_transaction(consistent_snapshot=True, readonly=True)
    try:
        cur = conn.cursor(dictionary=True, buffered=True)
        try:
            matched, problems = check_foreign_keys(_fetch(cur, SQL_FOREIGN_KEYS, ()))
            if problems:
                raise SyncRefused(
                    "The MariaDB schema does not have the foreign-key constraints the graph relationships rely on "
                    "(with the expected columns, order and grouping). Nothing was read further and nothing was "
                    "written to Neo4j.",
                    {"foreignKeyProblems": problems})
            src = tuple(GRAPH_SOURCES)
            snap = {
                "procurements": _fetch(cur, SQL_PROCUREMENTS, src),
                "bid_notices": _fetch(cur, SQL_BID_NOTICES, src),
                "bids": _fetch(cur, SQL_BIDS, src * 2),
                "bac_resolutions": _fetch(cur, SQL_RESOLUTIONS, src),
                "award_notices": _fetch(cur, SQL_AWARDS, src * 2),
            }
            procs = snap["procurements"]
            supplier_ids = [b["supplier_id"] for b in snap["bids"]] + [a["supplier_id"] for a in snap["award_notices"]]
            snap["suppliers"] = _fetch_by_ids(cur, "suppliers", supplier_ids)
            snap["procuring_entities"] = _fetch_by_ids(cur, "procuring_entities", [p["procuring_entity_id"] for p in procs])
            snap["categories"] = _fetch_by_ids(cur, "categories", [p["category_id"] for p in procs])
            snap["procurement_modes"] = _fetch_by_ids(cur, "procurement_modes", [p["mode_id"] for p in procs])
            snap["contracts"] = _fetch_by_ids(cur, "contracts", [a["contract_id"] for a in snap["award_notices"]])
            snap["supplier_representatives"] = _fetch_by_ids(
                cur, "supplier_representatives", [s["supplier_id"] for s in snap["suppliers"]])
            snap["persons"] = _fetch_by_ids(
                cur, "persons", [r["person_id"] for r in snap["supplier_representatives"]]
                + [a["certified_by_person_id"] for a in snap["award_notices"]])
        finally:
            cur.close()
    finally:
        conn.rollback()                       # read-only transaction: nothing to commit
    snap["_foreignKeysChecked"] = len(required_foreign_keys())
    snap["_foreignKeyConstraints"] = matched
    return snap


# ---------------------------------------------------------------- projection (pure Python)

def _where(table, column, row_id):
    return f"{table}.{column} (id {row_id})"


def _text(v, where):
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        try:
            return bytes(v).decode("utf-8")
        except UnicodeDecodeError:
            raise SyncDataError("Value in " + where + " is not valid text.") from None
    return str(v)


def _int(v, where):
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, Decimal) and v == v.to_integral_value():
        return int(v)
    if isinstance(v, str) and re.fullmatch(r"-?\d+", v.strip()):
        return int(v)
    raise SyncDataError("Value in " + where + " is not a whole number.")


def _num(v, where):
    if v is None:
        return None
    if isinstance(v, bool):
        raise SyncDataError("Value in " + where + " is not a number.")
    if isinstance(v, (int, float, Decimal)):
        return float(v)                       # same representation as GET /api/graph/sample
    if isinstance(v, str):
        try:
            return float(Decimal(v.strip()))
        except Exception:
            pass
    raise SyncDataError("Value in " + where + " is not a number.")


def _date(v, where):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v.strip()[:10])
        except ValueError:
            pass
    raise SyncDataError("Value in " + where + " is not a date.")


def _bool(v, where):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    i = _int(v, where)
    if i not in (0, 1):
        raise SyncDataError("Value in " + where + " is not 0 or 1.")
    return bool(i)


# node label -> (table, primary key column, [(property, column, converter)])
NODE_SOURCES = {
    "Procurement": ("procurements", "procurement_id", [
        ("referenceNo", "reference_no", _text), ("title", "title", _text), ("endUserUnit", "end_user_unit", _text),
        ("procurementClass", "procurement_class", _text), ("abc", "abc", _num), ("controlNo", "control_no", _text),
        ("targetTimeline", "target_timeline", _text), ("fiscalYear", "fiscal_year", _int),
        ("status", "status", _text), ("dataSource", "data_source", _text)]),
    "ProcuringEntity": ("procuring_entities", "entity_id", [
        ("name", "name", _text), ("city", "city", _text), ("province", "province", _text)]),
    "Category": ("categories", "category_id", [("name", "name", _text)]),
    "ProcurementMode": ("procurement_modes", "mode_id", [
        ("name", "name", _text), ("isCompetitive", "is_competitive", _bool)]),
    "BidNotice": ("bid_notices", "notice_id", [
        ("noticeType", "notice_type", _text), ("philgepsRef", "philgeps_ref", _text),
        ("publishedDate", "published_date", _date), ("submissionDeadline", "submission_deadline", _date),
        ("bidsReceived", "bids_received", _int)]),
    "Supplier": ("suppliers", "supplier_id", [
        ("name", "name", _text), ("address", "address", _text), ("city", "city", _text),
        ("province", "province", _text), ("dataSource", "data_source", _text)]),
    "Bid": ("bids", "bid_id", [("amount", "bid_amount", _num), ("rankNo", "rank_no", _int)]),
    "BacResolution": ("bac_resolutions", "resolution_id", [
        ("resolutionNo", "resolution_no", _text), ("recommendation", "recommendation", _text),
        ("postQualificationDone", "post_qualification_done", _bool), ("resolutionDate", "resolution_date", _date)]),
    "AwardNotice": ("award_notices", "award_id", [
        ("noticeNo", "notice_no", _text), ("bidReferenceNo", "bid_reference_no", _text),
        ("awardDate", "award_date", _date), ("publishedDate", "published_date", _date),
        ("awardedAmount", "awarded_amount", _num), ("awardBasis", "award_basis", _text),
        ("modeAsRecorded", "mode_as_recorded", _text), ("lotNo", "lot_no", _int),
        ("lotDescription", "lot_description", _text), ("lotQuantity", "lot_quantity", _text)]),
    "Contract": ("contracts", "contract_id", [("contractNo", "contract_no", _text), ("contractDate", "contract_date", _date)]),
    "Person": ("persons", "person_id", [("fullName", "full_name", _text)]),
}

# relationship type -> (table, row id column, from-key column, to-key column, [(property, column, converter)])
# REPRESENTED_BY has a composite key; its "row id" is reported as supplier_id/person_id.
REL_SOURCES = {
    "PROCURED_BY":         ("procurements", "procurement_id", "procurement_id", "procuring_entity_id", []),
    "IN_CATEGORY":         ("procurements", "procurement_id", "procurement_id", "category_id", []),
    "USES_MODE":           ("procurements", "procurement_id", "procurement_id", "mode_id", []),
    "NOTICE_FOR":          ("bid_notices", "notice_id", "notice_id", "procurement_id", []),
    "SUBMITTED":           ("bids", "bid_id", "supplier_id", "bid_id", []),
    "FOR_PROCUREMENT":     ("bids", "bid_id", "bid_id", "procurement_id", []),
    "UNDER_NOTICE":        ("bids", "bid_id", "bid_id", "notice_id", []),
    "RESOLVES":            ("bac_resolutions", "resolution_id", "resolution_id", "procurement_id", []),
    "RECOMMENDS":          ("bac_resolutions", "resolution_id", "resolution_id", "recommended_bid_id", []),
    "AWARDS":              ("award_notices", "award_id", "award_id", "procurement_id", []),
    "AWARDED_TO":          ("award_notices", "award_id", "award_id", "supplier_id", []),
    "BASED_ON_BID":        ("award_notices", "award_id", "award_id", "bid_id", []),
    "BASED_ON_RESOLUTION": ("award_notices", "award_id", "award_id", "resolution_id", []),
    "UNDER_CONTRACT":      ("award_notices", "award_id", "award_id", "contract_id", []),
    "CERTIFIED_BY":        ("award_notices", "award_id", "award_id", "certified_by_person_id",
                            [("role", "certifier_role", _text)]),
    "REPRESENTED_BY":      ("supplier_representatives", "supplier_id", "supplier_id", "person_id",
                            [("position", "position", _text)]),
}

if set(NODE_SOURCES) != set(NODE_KEYS) or set(REL_SOURCES) != set(REL_TYPES):
    raise RuntimeError("graph_sync: source tables do not match the graph tables")


def build_projection(snap):
    """Turn MariaDB rows into nodes and relationships. Nothing is inferred: one node per row,
    one relationship per non-NULL foreign key whose target row is in the synchronized set."""
    nodes = {}
    for label, (table, pk, fields) in NODE_SOURCES.items():
        out = {}
        for row in snap[table]:
            key = _int(row[pk], _where(table, pk, row[pk]))
            props = {prop: conv(row[col], _where(table, col, key)) for prop, col, conv in fields}
            if key in out and out[key] != props:
                raise SyncDataError(f"Two different rows share {table}.{pk} (id {key}).")
            out[key] = props
        nodes[label] = out

    rels, skipped = {}, {}
    for rtype, (table, id_col, from_col, to_col, fields) in REL_SOURCES.items():
        from_label, to_label, _fks = REL_TYPES[rtype]
        out, null_fk, out_of_scope = {}, 0, 0
        for row in snap[table]:
            where = _where(table, to_col, row[id_col])
            a = _int(row[from_col], where)
            b = _int(row[to_col], where)
            if a is None or b is None:
                null_fk += 1
                continue
            if a not in nodes[from_label] or b not in nodes[to_label]:
                out_of_scope += 1
                continue
            props = {prop: conv(row[col], where) for prop, col, conv in fields}
            if (a, b) in out and out[(a, b)] != props:
                raise SyncDataError(f"Two different {rtype} rows connect the same records ({where}).")
            out[(a, b)] = props
        rels[rtype] = out
        skipped[rtype] = {"skippedNullForeignKey": null_fk, "skippedOutOfScope": out_of_scope}
    return nodes, rels, skipped


# ---------------------------------------------------------------- Cypher (fixed text, values as parameters)

Stmt = namedtuple("Stmt", "op text meta")


def _merge_nodes(label):
    key = NODE_KEYS[label]
    return Stmt("merge_nodes",
                f"UNWIND $rows AS row "
                f"MERGE (n:`{label}` {{`{key}`: row.key}}) "
                f"SET n += row.props, n:`{OWNER_LABEL}`, n.syncOwner = $owner, n.syncRunId = $runId "
                f"RETURN count(n) AS n",
                {"label": label, "key": key})


def _merge_rels(rtype):
    a, b, _ = REL_TYPES[rtype]
    ka, kb = NODE_KEYS[a], NODE_KEYS[b]
    return Stmt("merge_rels",
                f"UNWIND $rows AS row "
                f"MATCH (a:`{a}` {{`{ka}`: row.src}}) "
                f"MATCH (b:`{b}` {{`{kb}`: row.dst}}) "
                f"MERGE (a)-[r:`{rtype}`]->(b) "
                f"SET r += row.props, r.syncOwner = $owner, r.syncRunId = $runId "
                f"RETURN count(r) AS n",
                {"type": rtype, "from": a, "to": b, "fromKey": ka, "toKey": kb})


def _foreign_nodes(label):
    return Stmt("foreign_nodes",
                f"MATCH (n:`{label}`) WHERE coalesce(n.syncOwner, '') <> $owner RETURN count(n) AS c",
                {"label": label})


def _foreign_rels(rtype):
    return Stmt("foreign_rels",
                f"MATCH ()-[r:`{rtype}`]->() WHERE coalesce(r.syncOwner, '') <> $owner RETURN count(r) AS c",
                {"type": rtype})


def _delete_stale_rels(rtype):
    return Stmt("delete_stale_rels",
                f"MATCH ()-[r:`{rtype}`]->() WHERE r.syncOwner = $owner AND r.syncRunId <> $runId "
                f"DELETE r RETURN count(*) AS removed",
                {"type": rtype})


def _stale_attached(label):
    return Stmt("stale_attached",
                f"MATCH (n:`{label}`) WHERE n.syncOwner = $owner AND n.syncRunId <> $runId "
                f"AND EXISTS {{ (n)--() }} RETURN count(n) AS c",
                {"label": label})


def _delete_stale_nodes(label):
    return Stmt("delete_stale_nodes",
                f"MATCH (n:`{label}`) WHERE n.syncOwner = $owner AND n.syncRunId <> $runId "
                f"DELETE n RETURN count(*) AS removed",
                {"label": label})


def _read_nodes(label):
    key = NODE_KEYS[label]
    return Stmt("read_nodes",
                f"MATCH (n:`{label}`) WHERE n.syncOwner = $owner RETURN n.`{key}` AS nodeKey, properties(n) AS props",
                {"label": label, "key": key})


def _read_rels(rtype):
    a, b, _ = REL_TYPES[rtype]
    return Stmt("read_rels",
                f"MATCH (a)-[r:`{rtype}`]->(b) WHERE r.syncOwner = $owner "
                f"RETURN labels(a) AS srcLabels, a.`{NODE_KEYS[a]}` AS srcKey, "
                f"labels(b) AS dstLabels, b.`{NODE_KEYS[b]}` AS dstKey, properties(r) AS props",
                {"type": rtype, "from": a, "to": b})


def _create_constraint(name):
    label, prop = CONSTRAINTS[name]
    return Stmt("create_constraint",
                f"CREATE CONSTRAINT `{name}` IF NOT EXISTS FOR (n:`{label}`) REQUIRE n.`{prop}` IS UNIQUE",
                {"name": name, "label": label, "prop": prop})


def _drop_constraint(name):
    return Stmt("drop_constraint", f"DROP CONSTRAINT `{name}` IF EXISTS", {"name": name})


def _count_label(label):
    return Stmt("count_label", f"MATCH (n:`{label}`) RETURN count(n) AS c", {"label": label})


SHOW_CONSTRAINTS = Stmt("show_constraints",
                        "SHOW CONSTRAINTS YIELD name, type, entityType, labelsOrTypes, properties "
                        "RETURN name, type, entityType, labelsOrTypes, properties", {})
INFO_LOCK = Stmt("info_lock",
                 f"MERGE (i:`{INFO_LABEL}` {{syncKey: $infoKey}}) "
                 f"ON CREATE SET i.syncOwner = $owner "
                 f"SET i.lockedByRun = $runId RETURN i.syncOwner AS owner", {})
INFO_RECORD_CONSTRAINTS = Stmt("info_record_constraints",
                               f"MERGE (i:`{INFO_LABEL}` {{syncKey: $infoKey}}) "
                               f"ON CREATE SET i.syncOwner = $owner "
                               f"WITH i WHERE i.syncOwner = $owner "
                               f"SET i.createdConstraints = [x IN coalesce(i.createdConstraints, []) WHERE NOT x IN $names] + $names "
                               f"RETURN i.createdConstraints AS names", {})
INFO_FINISH = Stmt("info_finish",
                   f"MATCH (i:`{INFO_LABEL}` {{syncKey: $infoKey}}) WHERE i.syncOwner = $owner "
                   f"SET i.lastRunId = $runId, i.lastSyncedAt = $syncedAt, i.lastSummary = $summary, "
                   f"i.lockedByRun = null RETURN count(i) AS c", {})
INFO_READ = Stmt("info_read",
                 f"MATCH (i:`{INFO_LABEL}`) RETURN i.syncKey AS infoKey, i.syncOwner AS owner, "
                 f"i.createdConstraints AS createdConstraints, i.lastRunId AS lastRunId, "
                 f"i.lastSyncedAt AS lastSyncedAt", {})
# --wipe checks: any "yes" means ownership is uncertain and the wipe refuses.
WIPE_MARKED_NOT_OWNED = Stmt("wipe_marked_not_owned",
                             f"MATCH (n:`{OWNER_LABEL}`) WHERE coalesce(n.syncOwner, '') <> $owner RETURN count(n) AS c", {})
WIPE_OWNED_BAD_LABELS = Stmt("wipe_owned_bad_labels",
                             f"MATCH (n:`{OWNER_LABEL}`) WHERE n.syncOwner = $owner AND "
                             f"(size([l IN labels(n) WHERE NOT l IN $allowed]) > 0 OR "
                             f"size([l IN labels(n) WHERE l IN $data]) <> 1) RETURN count(n) AS c", {})
WIPE_OWNED_UNMARKED = Stmt("wipe_owned_unmarked",
                           f"MATCH (n) WHERE n.syncOwner = $owner AND NOT n:`{OWNER_LABEL}` AND NOT n:`{INFO_LABEL}` "
                           f"RETURN count(n) AS c", {})
WIPE_FOREIGN_RELS = Stmt("wipe_foreign_rels",
                         f"MATCH (n)-[r]-() WHERE (n:`{OWNER_LABEL}` OR n:`{INFO_LABEL}`) AND n.syncOwner = $owner "
                         f"AND coalesce(r.syncOwner, '') <> $owner RETURN count(DISTINCT r) AS c", {})
WIPE_OWNED_RELS_LOOSE = Stmt("wipe_owned_rels_loose",
                             f"MATCH (a)-[r]->(b) WHERE r.syncOwner = $owner AND NOT "
                             f"(a:`{OWNER_LABEL}` AND a.syncOwner = $owner AND b:`{OWNER_LABEL}` AND b.syncOwner = $owner) "
                             f"RETURN count(r) AS c", {})
WIPE_COUNT_RELS = Stmt("wipe_count_rels",
                       "MATCH ()-[r]->() WHERE r.syncOwner = $owner RETURN type(r) AS relType, count(r) AS c", {})
WIPE_COUNT_NODES = Stmt("wipe_count_nodes",
                        f"MATCH (n:`{OWNER_LABEL}`) WHERE n.syncOwner = $owner "
                        f"RETURN [l IN labels(n) WHERE l <> '{OWNER_LABEL}'][0] AS nodeLabel, count(n) AS c", {})
WIPE_DELETE_RELS = Stmt("wipe_delete_rels",
                        "MATCH ()-[r]->() WHERE r.syncOwner = $owner DELETE r RETURN count(*) AS c", {})
WIPE_DELETE_NODES = Stmt("wipe_delete_nodes",
                         f"MATCH (n:`{OWNER_LABEL}`) WHERE n.syncOwner = $owner DELETE n RETURN count(*) AS c", {})
WIPE_DELETE_INFO = Stmt("wipe_delete_info",
                        f"MATCH (i:`{INFO_LABEL}` {{syncKey: $infoKey}}) WHERE i.syncOwner = $owner DELETE i RETURN count(*) AS c", {})


def _run(tx, stmt, params):
    """Run one statement in a transaction; returns (records, counters). (Replaced in simulated tests.)"""
    result = tx.run(stmt.text, params)
    records = [r.data() for r in result]
    return records, result.consume().counters


def _run_auto(session, stmt, params):
    """Run one auto-commit statement (schema commands). (Replaced in simulated tests.)"""
    result = session.run(stmt.text, params)
    records = [r.data() for r in result]
    return records, result.consume().counters


def _single(records, field):
    return records[0][field] if records else 0


def _chunks(items):
    for start in range(0, len(items), CHUNK):
        yield items[start:start + CHUNK]


# ---------------------------------------------------------------- Neo4j steps

def _check_foreign(tx):
    """Count nodes/relationships that use this module's labels/types but are not owned by it."""
    conflicts = {}
    base = {"owner": OWNER}
    for label in ALL_LABELS:
        c = _single(_run(tx, _foreign_nodes(label), base)[0], "c")
        if c:
            conflicts[f"node label {label}"] = c
    for rtype in REL_TYPES:
        c = _single(_run(tx, _foreign_rels(rtype), base)[0], "c")
        if c:
            conflicts[f"relationship type {rtype}"] = c
    return conflicts


def _refuse_foreign(conflicts):
    if conflicts:
        raise SyncRefused(
            "Neo4j already contains data with the labels or relationship types this sync uses, and it was not "
            "created by this sync. Nothing was written. Use an empty Neo4j database, or remove that data yourself.",
            {"conflicts": conflicts})


def _constraint_matches(row, label, prop):
    return (row.get("entityType") == "NODE" and list(row.get("labelsOrTypes") or []) == [label]
            and list(row.get("properties") or []) == [prop])


def _is_unique(row):
    return row.get("type") in ("UNIQUENESS", "NODE_PROPERTY_UNIQUENESS", "NODE_KEY")


def ensure_constraints(session):
    """Create the uniqueness constraints MERGE relies on; record the ones created by this module."""
    existing = _run_auto(session, SHOW_CONSTRAINTS, {})[0]
    created, present = [], []
    ordered = ["bac_pms_sync_info_key_unique"] + [n for n in CONSTRAINTS if n != "bac_pms_sync_info_key_unique"]
    try:
        for name in ordered:
            label, prop = CONSTRAINTS[name]
            same_name = [r for r in existing if r.get("name") == name]
            if same_name:
                if not (_is_unique(same_name[0]) and _constraint_matches(same_name[0], label, prop)):
                    raise SyncRefused(f"A Neo4j constraint named {name} exists with a different definition. "
                                      "Nothing was written.")
                present.append(name)
                continue
            if any(_is_unique(r) and _constraint_matches(r, label, prop) for r in existing):
                present.append(name + " (equivalent constraint under another name; not owned)")
                continue
            try:
                _run_auto(session, _create_constraint(name), {})
            except Exception as exc:
                if _is_connection_error(exc):
                    raise
                _log.warning("Neo4j constraint %s not created (%s %s)", name, type(exc).__name__, neo4j_db._neo4j_code(exc))
                raise SyncRefused(f"Neo4j did not allow the uniqueness constraint {name} on :{label}({prop}). "
                                  "An existing index or duplicate values may prevent it. Nothing else was written.")
            created.append(name)
    finally:
        if created:                           # record ownership even if a later constraint failed
            session.execute_write(lambda tx: _run(tx, INFO_RECORD_CONSTRAINTS,
                                                  {"infoKey": INFO_KEY, "owner": OWNER, "names": created}))
    return {"created": created, "alreadyPresent": present}


def _write(tx, nodes, rels, run_id, report):
    """The single write transaction. Raises SyncRefused to roll everything back."""
    _refuse_foreign(_check_foreign(tx))
    owner = _single(_run(tx, INFO_LOCK, {"infoKey": INFO_KEY, "owner": OWNER, "runId": run_id})[0], "owner")
    if owner != OWNER:
        raise SyncRefused("The Neo4j sync record is not owned by this application. Nothing was written.")
    base = {"owner": OWNER, "runId": run_id}

    for label in DATA_LABELS:
        rows = [{"key": k, "props": p} for k, p in sorted(nodes[label].items())]
        created = 0
        for part in _chunks(rows):
            recs, counters = _run(tx, _merge_nodes(label), dict(base, rows=part))
            if _single(recs, "n") != len(part):
                raise SyncRefused(f"Neo4j did not confirm every {label} node. Nothing was written.")
            created += counters.nodes_created
        report["nodes"][label] = {"rows": len(rows), "created": created, "matched": len(rows) - created}

    for rtype in REL_TYPES:
        rows = [{"src": a, "dst": b, "props": p} for (a, b), p in sorted(rels[rtype].items())]
        created = 0
        for part in _chunks(rows):
            recs, counters = _run(tx, _merge_rels(rtype), dict(base, rows=part))
            if _single(recs, "n") != len(part):
                raise SyncRefused(f"Neo4j could not connect every {rtype} relationship. Nothing was written.")
            created += counters.relationships_created
        entry = {"rows": len(rows), "created": created, "matched": len(rows) - created}
        entry.update(report["_skipped"][rtype])
        report["relationships"][rtype] = entry

    removed_rels, removed_nodes = {}, {}
    for rtype in REL_TYPES:
        n = _single(_run(tx, _delete_stale_rels(rtype), base)[0], "removed")
        if n:
            removed_rels[rtype] = n
    attached = {label: c for label in DATA_LABELS
                if (c := _single(_run(tx, _stale_attached(label), base)[0], "c"))}
    if attached:
        raise SyncRefused("Records that MariaDB no longer has are still connected in Neo4j to data this sync did "
                          "not create, so they were not removed. Nothing was written.", {"staleConnectedNodes": attached})
    for label in DATA_LABELS:
        n = _single(_run(tx, _delete_stale_nodes(label), base)[0], "removed")
        if n:
            removed_nodes[label] = n
    report["removedStale"] = {"nodes": removed_nodes, "relationships": removed_rels}


def _plain(value):
    """Neo4j temporal values -> Python values, for comparison."""
    if hasattr(value, "to_native"):
        return value.to_native()
    return value


def _verify(tx, nodes, rels):
    """Read the owned projection back and compare it with what MariaDB produced."""
    problems, checked_nodes, checked_rels = [], 0, 0
    marker = {"syncOwner", "syncRunId"}
    for label in DATA_LABELS:
        key = NODE_KEYS[label]
        found = {}
        dupes = set()
        for r in _run(tx, _read_nodes(label), {"owner": OWNER})[0]:
            props = {k: _plain(v) for k, v in (r["props"] or {}).items() if k not in marker and k != key}
            if r["nodeKey"] in found:
                dupes.add(r["nodeKey"])
            found[r["nodeKey"]] = props
        expected = {k: {p: v for p, v in props.items() if v is not None} for k, props in nodes[label].items()}
        missing = sorted(set(expected) - set(found))
        extra = sorted(set(found) - set(expected))
        different = sorted(k for k in set(expected) & set(found) if expected[k] != found[k])
        for kind, keys in (("missing", missing), ("unexpected", extra), ("different", different), ("duplicate", sorted(dupes))):
            if keys:
                problems.append({"label": label, "problem": kind, "count": len(keys), "keys": keys[:10]})
        checked_nodes += len(found)
    for rtype in REL_TYPES:
        a, b, _ = REL_TYPES[rtype]
        found, dupes, wrong_ends = {}, set(), 0
        for r in _run(tx, _read_rels(rtype), {"owner": OWNER})[0]:
            if a not in (r["srcLabels"] or []) or b not in (r["dstLabels"] or []):
                wrong_ends += 1
                continue
            pair = (r["srcKey"], r["dstKey"])
            if pair in found:
                dupes.add(pair)
            found[pair] = {k: _plain(v) for k, v in (r["props"] or {}).items() if k not in marker}
        expected = {pair: {p: v for p, v in props.items() if v is not None} for pair, props in rels[rtype].items()}
        missing = sorted(set(expected) - set(found))
        extra = sorted(set(found) - set(expected))
        different = sorted(p for p in set(expected) & set(found) if expected[p] != found[p])
        for kind, keys in (("missing", missing), ("unexpected", extra), ("different", different), ("duplicate", sorted(dupes))):
            if keys:
                problems.append({"relationship": rtype, "problem": kind, "count": len(keys),
                                 "keys": [list(k) for k in keys[:10]]})
        if wrong_ends:
            problems.append({"relationship": rtype, "problem": "wrong endpoint labels", "count": wrong_ends})
        checked_rels += len(found)
    return {"ok": not problems, "checked": {"nodes": checked_nodes, "relationships": checked_rels}, "problems": problems}


# ---------------------------------------------------------------- orchestration and error handling

_sync_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _is_neo4j_error(exc):
    return neo4j_db.neo4j is not None and isinstance(
        exc, (neo4j_db.Neo4jError, neo4j_db.neo4j.exceptions.DriverError))


def _is_connection_error(exc):
    return neo4j_db.neo4j is not None and isinstance(
        exc, (neo4j_db.ServiceUnavailable, neo4j_db.AuthError))


def _neo4j_failure(exc, stage):
    """Map a Neo4j driver error to (http status, safe body). Logs class/code only."""
    code = neo4j_db._neo4j_code(exc)
    if neo4j_db.neo4j is not None and isinstance(exc, neo4j_db.AuthError):
        neo4j_db.close_driver()
        state, status, msg = "auth_failed", 503, "Neo4j rejected the configured user name or password."
    elif neo4j_db.neo4j is not None and isinstance(exc, neo4j_db.ServiceUnavailable):
        state, status, msg = "unreachable", 503, "Neo4j is not reachable. Check that the database is started."
    elif code.endswith("DatabaseNotFound"):
        state, status, msg = "database_not_found", 503, "The database named in NEO4J_DATABASE does not exist."
    else:
        state, status, msg = "error", 500, "Neo4j reported an error. Nothing from this run was kept (the transaction was rolled back)."
    _log.warning("Neo4j %s failed (%s%s)", stage, type(exc).__name__, (", " + code) if code else "")
    return status, _failure("error", stage, msg, neo4j=state)


def _failure(status, stage, message, **extra):
    body = {"status": status, "stage": stage, "message": message, "errors": [{"stage": stage, "message": message}]}
    body.update(extra)
    return body


def _open_neo4j():
    """(settings, driver) or raises a tuple (http status, body)."""
    if neo4j_db.neo4j is None:
        raise _Stop(503, _failure("error", "neo4j_connect", "The Neo4j Python driver is not installed.",
                                  neo4j="driver_missing"))
    try:
        settings = neo4j_db.read_settings()
    except neo4j_db.Neo4jSettingsError as exc:
        raise _Stop(503, _failure("error", "neo4j_connect", str(exc), neo4j="not_configured"))
    return settings, neo4j_db.get_driver(settings)


class _Stop(Exception):
    def __init__(self, status, body):
        super().__init__(body.get("message"))
        self.status, self.body = status, body


def _read_mariadb():
    conn = None
    try:
        conn = get_db_connection()
        snap = read_snapshot(conn)
        nodes, rels, skipped = build_projection(snap)
        return snap, nodes, rels, skipped
    except SyncRefused as exc:
        raise _Stop(409, _failure("refused", "read_mariadb", str(exc), **exc.details))
    except SyncDataError as exc:
        raise _Stop(500, _failure("error", "read_mariadb", str(exc) + " Nothing was written to Neo4j."))
    except Exception as exc:
        _log.warning("MariaDB read for the graph sync failed (%s%s)", type(exc).__name__,
                     f", errno {exc.errno}" if getattr(exc, "errno", None) else "")
        raise _Stop(500, _failure("error", "read_mariadb",
                                  "Could not read the approved records from MariaDB. Nothing was written to Neo4j."))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def run_sync():
    """Synchronize once. Returns (http status, body). Never raises."""
    if not _sync_lock.acquire(blocking=False):
        return 409, _failure("refused", "start", "A synchronization is already running. Try again when it finishes.")
    started, t0 = _now(), time.monotonic()
    run_id = uuid.uuid4().hex
    stage = "read_mariadb"
    try:
        snap, nodes, rels, skipped = _read_mariadb()
        stage = "neo4j_connect"
        settings, driver = _open_neo4j()
        report = {"nodes": {}, "relationships": {}, "_skipped": skipped}
        with driver.session(database=settings.database) as session:
            stage = "neo4j_preflight"
            _refuse_foreign(session.execute_read(_check_foreign))
            stage = "neo4j_constraints"
            constraints = ensure_constraints(session)
            stage = "neo4j_write"
            session.execute_write(_with_timeout(lambda tx: _write(tx, nodes, rels, run_id, report)))
            stage = "neo4j_verify"
            verification = session.execute_read(lambda tx: _verify(tx, nodes, rels))
            report.pop("_skipped")
            body = {
                "status": "success" if verification["ok"] else "error",
                "message": ("Neo4j now matches the approved MariaDB records." if verification["ok"] else
                            "The sync was written, but the read-back does not match MariaDB. See verification."),
                "runId": run_id,
                "startedAt": started,
                "source": {"database": EXPECTED_DATABASE, "dataSources": list(GRAPH_SOURCES),
                           "foreignKeysChecked": snap["_foreignKeysChecked"],
                           "foreignKeyConstraints": snap["_foreignKeyConstraints"],
                           "rows": {t: len(snap[t]) for t in sorted(k for k in snap if not k.startswith("_"))}},
                "target": {"database": settings.database},
                "constraints": constraints,
                **report,
                "totals": _totals(report),
                "verification": verification,
                "errors": [] if verification["ok"] else [{"stage": "neo4j_verify", "message": "Read-back mismatch."}],
            }
            body["finishedAt"] = _now()
            body["durationMs"] = int((time.monotonic() - t0) * 1000)
            stage = "neo4j_record"
            session.execute_write(lambda tx: _run(tx, INFO_FINISH, {
                "infoKey": INFO_KEY, "owner": OWNER, "runId": run_id, "syncedAt": body["finishedAt"],
                "summary": json.dumps({"totals": body["totals"], "verificationOk": verification["ok"]})}))
        return (200 if verification["ok"] else 500), body
    except _Stop as stop:
        return stop.status, stop.body
    except SyncRefused as exc:
        return 409, _failure("refused", stage, str(exc), **exc.details)
    except Exception as exc:
        if _is_neo4j_error(exc):
            return _neo4j_failure(exc, stage)
        _log.warning("Graph sync failed at %s (%s)", stage, type(exc).__name__)
        return 500, _failure("error", stage, "The synchronization failed. See the Flask terminal for the error type.")
    finally:
        _sync_lock.release()


def _with_timeout(fn):
    if neo4j_db.neo4j is not None and hasattr(neo4j_db.neo4j, "unit_of_work"):
        return neo4j_db.neo4j.unit_of_work(timeout=WRITE_TIMEOUT_SECONDS)(fn)
    return fn


def _totals(report):
    n, r = report["nodes"].values(), report["relationships"].values()
    stale = report.get("removedStale", {"nodes": {}, "relationships": {}})
    return {
        "nodesCreated": sum(x["created"] for x in n), "nodesMatched": sum(x["matched"] for x in n),
        "relationshipsCreated": sum(x["created"] for x in r), "relationshipsMatched": sum(x["matched"] for x in r),
        "relationshipsSkipped": sum(x["skippedNullForeignKey"] + x["skippedOutOfScope"] for x in r),
        "nodesRemoved": sum(stale["nodes"].values()), "relationshipsRemoved": sum(stale["relationships"].values()),
    }


def run_verify():
    """Read-only comparison of Neo4j with MariaDB. Returns (http status, body)."""
    stage = "read_mariadb"
    try:
        _snap, nodes, rels, _skipped = _read_mariadb()
        stage = "neo4j_connect"
        settings, driver = _open_neo4j()
        stage = "neo4j_verify"
        with driver.session(database=settings.database) as session:
            conflicts = session.execute_read(_check_foreign)
            verification = session.execute_read(lambda tx: _verify(tx, nodes, rels))
            info = session.execute_read(lambda tx: _run(tx, INFO_READ, {})[0])
        ok = verification["ok"] and not conflicts
        return (200 if ok else 500), {
            "status": "success" if ok else "error",
            "message": "Neo4j matches the approved MariaDB records." if ok else "Neo4j does not match MariaDB.",
            "lastSync": ({"runId": info[0].get("lastRunId"), "syncedAt": info[0].get("lastSyncedAt")} if info else None),
            "foreignData": conflicts, "verification": verification}
    except _Stop as stop:
        return stop.status, stop.body
    except Exception as exc:
        if _is_neo4j_error(exc):
            return _neo4j_failure(exc, stage)
        _log.warning("Graph verify failed at %s (%s)", stage, type(exc).__name__)
        return 500, _failure("error", stage, "The verification failed. See the terminal for the error type.")


def _wipe_checks(tx):
    p = {"owner": OWNER, "allowed": DATA_LABELS + [OWNER_LABEL], "data": DATA_LABELS}
    problems = {}
    for stmt, text in ((WIPE_MARKED_NOT_OWNED, f"nodes with the {OWNER_LABEL} label but another or no owner"),
                       (WIPE_OWNED_BAD_LABELS, "owned nodes with extra or missing labels"),
                       (WIPE_OWNED_UNMARKED, f"nodes with the owner property but without the {OWNER_LABEL} label"),
                       (WIPE_FOREIGN_RELS, "relationships on owned nodes that this sync did not create"),
                       (WIPE_OWNED_RELS_LOOSE, "owned relationships that touch nodes this sync does not own")):
        c = _single(_run(tx, stmt, p)[0], "c")
        if c:
            problems[text] = c
    info = _run(tx, INFO_READ, {})[0]
    if len(info) > 1 or any(i.get("owner") != OWNER or i.get("infoKey") != INFO_KEY for i in info):
        problems[f"{INFO_LABEL} nodes not created by this sync"] = len(info)
    nodes = {r["nodeLabel"]: r["c"] for r in _run(tx, WIPE_COUNT_NODES, p)[0]}
    rels = {r["relType"]: r["c"] for r in _run(tx, WIPE_COUNT_RELS, p)[0]}
    recorded = list(info[0].get("createdConstraints") or []) if len(info) == 1 and info[0].get("owner") == OWNER else []
    return problems, nodes, rels, recorded, len(info)


def run_wipe(confirm=False):
    """Remove the projection from Neo4j. Dry run unless confirm=True. Returns (exit code, body)."""
    try:
        settings, driver = _open_neo4j()
        with driver.session(database=settings.database) as session:
            problems, nodes, rels, recorded, info_count = session.execute_read(_wipe_checks)
            existing = {r["name"]: r for r in _run_auto(session, SHOW_CONSTRAINTS, {})[0]}
            drop, keep = [], {}
            for name in recorded:
                if name not in CONSTRAINTS:
                    keep[name] = "not one of this module's constraint names"
                elif name not in existing:
                    keep[name] = "no longer exists"
                elif not (_is_unique(existing[name]) and _constraint_matches(existing[name], *CONSTRAINTS[name])):
                    keep[name] = "definition changed since it was created"
                else:
                    drop.append(name)
            for name in existing:
                if name.startswith("bac_pms_") and name not in recorded:
                    keep[name] = "not recorded as created by this sync"
            plan = {"database": settings.database, "nodes": nodes, "relationships": rels,
                    "syncInfoNodes": info_count, "constraintsToDrop": drop, "constraintsKept": keep}
            if problems:
                return 2, {"status": "refused", "message": "Ownership is uncertain, so nothing was removed.",
                           "problems": problems, "plan": plan}
            if not confirm:
                return 0, {"status": "dry_run", "message": "Nothing was removed. Run again with --yes to remove this.",
                           "plan": plan}

            def wipe(tx):
                again = _wipe_checks(tx)[0]
                if again:
                    raise SyncRefused("Ownership became uncertain during the wipe. Nothing was removed.", {"problems": again})
                r = _single(_run(tx, WIPE_DELETE_RELS, {"owner": OWNER})[0], "c")
                n = _single(_run(tx, WIPE_DELETE_NODES, {"owner": OWNER})[0], "c")
                i = _single(_run(tx, WIPE_DELETE_INFO, {"owner": OWNER, "infoKey": INFO_KEY})[0], "c")
                return {"relationships": r, "nodes": n, "syncInfo": i}
            removed = session.execute_write(_with_timeout(wipe))

            dropped = []
            for name in drop:
                label = CONSTRAINTS[name][0]
                left = _single(_run_auto(session, _count_label(label), {})[0], "c")
                if left:
                    keep[name] = f"{left} node(s) not owned by this sync still use :{label}"
                    continue
                _run_auto(session, _drop_constraint(name), {})
                dropped.append(name)
            return 0, {"status": "success", "message": "The Neo4j projection was removed. MariaDB was not touched.",
                       "removed": removed, "constraintsDropped": dropped, "constraintsKept": keep}
    except _Stop as stop:
        return 1, stop.body
    except SyncRefused as exc:
        return 2, _failure("refused", "wipe", str(exc), **exc.details)
    except Exception as exc:
        if _is_neo4j_error(exc):
            return 1, _neo4j_failure(exc, "wipe")[1]
        _log.warning("Graph wipe failed (%s)", type(exc).__name__)
        return 1, _failure("error", "wipe", "The wipe failed. See the terminal for the error type.")


def main(argv):
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    args = set(argv)
    unknown = args - {"--verify", "--wipe", "--yes"}
    if unknown or ("--yes" in args and "--wipe" not in args) or {"--verify", "--wipe"} <= args:
        print("Usage: python graph_sync.py [--verify | --wipe [--yes]]")
        return 64
    if "--wipe" in args:
        code, body = run_wipe(confirm="--yes" in args)
    elif "--verify" in args:
        status, body = run_verify()
        code = 0 if status == 200 else 1
    else:
        status, body = run_sync()
        code = 0 if status == 200 else (2 if status == 409 else 1)
    print(json.dumps(body, indent=2, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
