"""
backend/t_graph_sync.py - ISOLATED, SIMULATED tests for graph_sync.py.

Run from backend/:   python t_graph_sync.py

Safety (this file never touches a real database):
  * Before graph_sync is imported, a stand-in module is installed as "db", so the real backend/db.py
    (mysql.connector + config.DB_CONFIG) is never imported. Any attempt to open a MariaDB connection
    through it raises immediately.
  * MariaDB is replaced by FakeMariaDB: it returns fixed result sets, accepts SELECT statements only,
    and fails the test on any commit.
  * Neo4j is replaced by FakeGraph, an in-memory stand-in that implements only the statements
    graph_sync sends (MERGE, SET += with null removing a property, DELETE failing while relationships
    remain, transaction rollback). neo4j_db.GraphDatabase is replaced so no real driver can be created.
  * NEO4J_* variables are overwritten in this process only with dummy values.
What is real: graph_sync.py, graph_api.py (for GRAPH_SOURCES), neo4j_db.py, auth.py imports, and the
neo4j package's exception classes. Everything about MariaDB and Neo4j behaviour is SIMULATED.
"""
import copy
import json
import os
import re
import sys
import types
from datetime import date
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# ---- 1. Make real MariaDB unreachable for this process -------------------------------------
if "db" in sys.modules:
    sys.exit("TEST SAFETY: the real db module is already imported; run this file on its own.")
REAL_DB_ATTEMPTS = []


def _forbidden_db(*_a, **_k):
    REAL_DB_ATTEMPTS.append(1)
    raise AssertionError("TEST SAFETY: a real MariaDB connection was attempted")


_fake_db_module = types.ModuleType("db")
_fake_db_module.get_db_connection = _forbidden_db
sys.modules["db"] = _fake_db_module

# ---- 2. Make real Neo4j unreachable for this process ---------------------------------------
os.environ.update(NEO4J_URI="bolt://127.0.0.1:9", NEO4J_USERNAME="test-user-not-real",
                  NEO4J_PASSWORD="test-password-not-real", NEO4J_DATABASE="neo4j")
import neo4j_db  # noqa: E402

if neo4j_db.neo4j is None:
    sys.exit("The neo4j driver package is required for these tests (exception classes). "
             'Install it with: pip install "neo4j==6.4.1"')
REAL_DRIVER_ATTEMPTS = []


class _ForbiddenGraphDatabase:
    @staticmethod
    def driver(*_a, **_k):
        REAL_DRIVER_ATTEMPTS.append(1)
        raise AssertionError("TEST SAFETY: a real Neo4j driver was requested")


neo4j_db.GraphDatabase = _ForbiddenGraphDatabase

import graph_sync as GS  # noqa: E402  (imports graph_api -> auth -> the stand-in db module)
from neo4j.exceptions import ClientError, ServiceUnavailable  # noqa: E402

if GS.get_db_connection is not _forbidden_db:
    sys.exit("TEST SAFETY: graph_sync did not pick up the stand-in db module")

# ---- 3. Foreign-key metadata, written out from the schema (independent of graph_sync) -----
# (name, table, columns, referenced table, referenced columns)
SCHEMA_FKS = [
    ("fk_proc_entity", "procurements", ["procuring_entity_id"], "procuring_entities", ["entity_id"]),
    ("fk_proc_category", "procurements", ["category_id"], "categories", ["category_id"]),
    ("fk_proc_mode", "procurements", ["mode_id"], "procurement_modes", ["mode_id"]),
    ("fk_proc_created_by", "procurements", ["created_by"], "users", ["user_id"]),            # not used by the graph
    ("fk_notices_procurement", "bid_notices", ["procurement_id"], "procurements", ["procurement_id"]),
    ("fk_bids_procurement", "bids", ["procurement_id"], "procurements", ["procurement_id"]),
    ("fk_bids_notice_same_proc", "bids", ["notice_id", "procurement_id"], "bid_notices", ["notice_id", "procurement_id"]),
    ("fk_bids_supplier", "bids", ["supplier_id"], "suppliers", ["supplier_id"]),
    ("fk_bids_recorded_by", "bids", ["recorded_by"], "users", ["user_id"]),                  # not used by the graph
    ("fk_res_procurement", "bac_resolutions", ["procurement_id"], "procurements", ["procurement_id"]),
    ("fk_res_bid_same_proc", "bac_resolutions", ["recommended_bid_id", "procurement_id"], "bids", ["bid_id", "procurement_id"]),
    ("fk_res_logged_by", "bac_resolutions", ["logged_by"], "users", ["user_id"]),            # not used by the graph
    ("fk_awards_procurement", "award_notices", ["procurement_id"], "procurements", ["procurement_id"]),
    ("fk_awards_supplier", "award_notices", ["supplier_id"], "suppliers", ["supplier_id"]),
    ("fk_awards_contract", "award_notices", ["contract_id"], "contracts", ["contract_id"]),
    ("fk_awards_bid_same_proc_supplier", "award_notices", ["bid_id", "procurement_id", "supplier_id"],
     "bids", ["bid_id", "procurement_id", "supplier_id"]),
    ("fk_awards_resolution_same_proc", "award_notices", ["resolution_id", "procurement_id"],
     "bac_resolutions", ["resolution_id", "procurement_id"]),
    ("fk_awards_certifier", "award_notices", ["certified_by_person_id"], "persons", ["person_id"]),
    ("fk_awards_issued_by", "award_notices", ["issued_by"], "users", ["user_id"]),           # not used by the graph
    ("fk_reps_supplier", "supplier_representatives", ["supplier_id"], "suppliers", ["supplier_id"]),
    ("fk_reps_person", "supplier_representatives", ["person_id"], "persons", ["person_id"]),
]
COMPOSITES = ["fk_awards_bid_same_proc_supplier", "fk_awards_resolution_same_proc",
              "fk_res_bid_same_proc", "fk_bids_notice_same_proc"]
REQUIRED_SINGLE = ["fk_proc_entity", "fk_proc_category", "fk_proc_mode", "fk_notices_procurement",
                   "fk_bids_procurement", "fk_bids_supplier", "fk_res_procurement", "fk_awards_procurement",
                   "fk_awards_supplier", "fk_awards_contract", "fk_awards_certifier", "fk_reps_supplier", "fk_reps_person"]


def kcu_rows(fks, positions=None):
    """information_schema.KEY_COLUMN_USAGE rows (dictionary cursor) for a list of constraints."""
    rows = []
    for name, table, cols, ref_table, ref_cols in fks:
        pos = (positions or {}).get(name, list(range(1, len(cols) + 1)))
        for c, rc, p in zip(cols, ref_cols, pos):
            rows.append({"CONSTRAINT_NAME": name, "TABLE_NAME": table, "COLUMN_NAME": c, "ORDINAL_POSITION": p,
                         "REFERENCED_TABLE_NAME": ref_table, "REFERENCED_COLUMN_NAME": rc})
    return rows


def without(name):
    return [f for f in SCHEMA_FKS if f[0] != name]


def replace(name, *new):
    out = []
    for f in SCHEMA_FKS:
        out.extend(new if f[0] == name else [f])
    return out


def get(name):
    return next(f for f in SCHEMA_FKS if f[0] == name)


def split(name):
    n, t, cols, rt, rcols = get(name)
    return replace(name, *[(f"{n}_part{i + 1}", t, [c], rt, [rc]) for i, (c, rc) in enumerate(zip(cols, rcols))])


def drop_last_column(name):
    n, t, cols, rt, rcols = get(name)
    return replace(name, (n, t, cols[:-1], rt, rcols[:-1]))


def reversed_order(name):
    n, t, cols, rt, rcols = get(name)
    return replace(name, (n, t, cols[::-1], rt, rcols[::-1]))


def crossed(name):
    n, t, cols, rt, rcols = get(name)
    return replace(name, (n, t, cols, rt, rcols[::-1]))


def wrong_ref_table(name):
    n, t, cols, rt, rcols = get(name)
    return replace(name, (n, t, cols, "procurements", rcols))


# ---- 4. Fixed MariaDB result sets for the approved sample scope (values typed as mysql.connector returns them)
D = Decimal
TABLES = {
    "procurements": [
        dict(procurement_id=101, reference_no="SR-2026-101", title="Office supplies", procuring_entity_id=1,
             end_user_unit="GSO", procurement_class="Goods", category_id=5, mode_id=1, abc=D("150000.00"),
             control_no=None, target_timeline="Nov 2026", fiscal_year=2026, status="Awarded", data_source="sample"),
        dict(procurement_id=102, reference_no="SR-2026-102", title="Road repair", procuring_entity_id=1,
             end_user_unit="CEO", procurement_class="Infrastructure", category_id=None, mode_id=2, abc=D("2500000.50"),
             control_no="CN-7", target_timeline=None, fiscal_year=2026, status="Failed Bidding", data_source="sample"),
        dict(procurement_id=103, reference_no="SR-2026-103", title="Laptops", procuring_entity_id=1,
             end_user_unit="MIS", procurement_class="Goods", category_id=6, mode_id=1, abc=D("480000.00"),
             control_no=None, target_timeline="Dec 2026", fiscal_year=2026, status="Awarded", data_source="sample"),
    ],
    "bid_notices": [
        dict(notice_id=201, procurement_id=101, notice_type="RFQ", philgeps_ref="PG-201", published_date=date(2026, 9, 1),
             submission_deadline=date(2026, 9, 15), bids_received=2),
        dict(notice_id=202, procurement_id=102, notice_type="ITB", philgeps_ref=None, published_date=None,
             submission_deadline=date(2026, 9, 20), bids_received=1),
        dict(notice_id=203, procurement_id=103, notice_type="RFQ", philgeps_ref="PG-203", published_date=date(2026, 9, 5),
             submission_deadline=date(2026, 9, 25), bids_received=None),
    ],
    "bids": [
        dict(bid_id=501, procurement_id=101, notice_id=201, supplier_id=301, bid_amount=D("140000.00"), rank_no=1),
        dict(bid_id=502, procurement_id=101, notice_id=201, supplier_id=302, bid_amount=D("145500.25"), rank_no=2),
        dict(bid_id=503, procurement_id=102, notice_id=202, supplier_id=303, bid_amount=D("2600000.00"), rank_no=None),
        dict(bid_id=504, procurement_id=103, notice_id=203, supplier_id=301, bid_amount=D("470000.00"), rank_no=1),
    ],
    "bac_resolutions": [
        dict(resolution_id=601, procurement_id=101, resolution_no="R-601", recommendation="LCRB", recommended_bid_id=501,
             post_qualification_done=1, resolution_date=date(2026, 9, 20)),
        dict(resolution_id=602, procurement_id=102, resolution_no="R-602", recommendation="FAIL", recommended_bid_id=None,
             post_qualification_done=0, resolution_date=None),
        # recommends a bid outside the approved scope (e.g. a PhilGEPS supplier's bid): no relationship
        dict(resolution_id=603, procurement_id=103, resolution_no="R-603", recommendation="LCRB", recommended_bid_id=999,
             post_qualification_done=1, resolution_date=date(2026, 9, 28)),
    ],
    "award_notices": [
        dict(award_id=701, procurement_id=101, supplier_id=301, contract_id=801, bid_id=501, resolution_id=601,
             notice_no="NOA-701", bid_reference_no=None, award_date=date(2026, 9, 25), published_date=date(2026, 9, 26),
             awarded_amount=D("140000.00"), award_basis="LCRB", mode_as_recorded=None, lot_no=None, lot_description=None,
             lot_quantity=None, certified_by_person_id=403, certifier_role="BAC Chairperson"),
        dict(award_id=702, procurement_id=103, supplier_id=301, contract_id=None, bid_id=None, resolution_id=None,
             notice_no="NOA-702", bid_reference_no=None, award_date=None, published_date=None,
             awarded_amount=D("470000.00"), award_basis=None, mode_as_recorded=None, lot_no=None, lot_description=None,
             lot_quantity=None, certified_by_person_id=None, certifier_role=None),
    ],
    "suppliers": [
        dict(supplier_id=301, name="Alpha Trading", address="1 Rizal St", city="Santa Rosa", province="Laguna", data_source="sample"),
        dict(supplier_id=302, name="Beta Supply", address=None, city="Biñan", province="Laguna", data_source="sample"),
        dict(supplier_id=303, name="Gamma Builders", address=None, city=None, province=None, data_source="sample"),
        dict(supplier_id=304, name="Unused Sample Co", address=None, city=None, province=None, data_source="sample"),
    ],
    "procuring_entities": [dict(entity_id=1, name="City Government of Santa Rosa", city="Santa Rosa", province="Laguna")],
    "categories": [dict(category_id=5, name="Office Supplies"), dict(category_id=6, name="ICT Equipment"),
                   dict(category_id=7, name="Unused Category")],
    "procurement_modes": [dict(mode_id=1, name="Small Value Procurement", is_competitive=0),
                          dict(mode_id=2, name="Public Bidding", is_competitive=1)],
    "contracts": [dict(contract_id=801, contract_no="PO-801", contract_date=date(2026, 9, 30))],
    "supplier_representatives": [dict(supplier_id=301, person_id=401, position="Owner"),
                                 dict(supplier_id=302, person_id=402, position=None),
                                 dict(supplier_id=304, person_id=404, position="Manager")],
    "persons": [dict(person_id=401, full_name="Ana Cruz"), dict(person_id=402, full_name="Ben Reyes"),
                dict(person_id=403, full_name="Carla Santos"), dict(person_id=404, full_name="Unused Person")],
}
BY_ID_KEY = {"suppliers": "supplier_id", "procuring_entities": "entity_id", "categories": "category_id",
             "procurement_modes": "mode_id", "contracts": "contract_id",
             "supplier_representatives": "supplier_id", "persons": "person_id"}
MAIN_SQL = {"procurements": GS.SQL_PROCUREMENTS, "bid_notices": GS.SQL_BID_NOTICES, "bids": GS.SQL_BIDS,
            "bac_resolutions": GS.SQL_RESOLUTIONS, "award_notices": GS.SQL_AWARDS}


class FakeMariaDB:
    """Stand-in for a mysql.connector connection: fixed result sets, SELECT only, no commit."""
    def __init__(self, fk_rows, database=GS.EXPECTED_DATABASE):
        self.fk_rows, self.database = fk_rows, database
        self.queries, self.started, self.rollbacks, self.closed = [], [], 0, False

    def cursor(self, dictionary=False, buffered=False):
        return FakeCursor(self, dictionary)

    def start_transaction(self, consistent_snapshot=False, isolation_level=None, readonly=None):
        self.started.append({"consistent_snapshot": consistent_snapshot, "readonly": readonly})

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        raise AssertionError("TEST SAFETY: graph_sync tried to COMMIT on MariaDB")

    def close(self):
        self.closed = True


class FakeCursor:
    def __init__(self, conn, dictionary):
        self.conn, self.dictionary, self.rows = conn, dictionary, []

    def execute(self, sql, params=()):
        params = tuple(params)
        if not sql.lstrip().upper().startswith("SELECT"):
            raise AssertionError("TEST SAFETY: non-SELECT statement sent to MariaDB")
        self.conn.queries.append(sql)
        if sql == "SELECT DATABASE()":
            self.rows = [(self.conn.database,)]
            return
        if sql == GS.SQL_FOREIGN_KEYS:
            assert params == () and self.dictionary
            self.rows = [dict(r) for r in self.conn.fk_rows]
            return
        for table, text in MAIN_SQL.items():
            if sql == text:
                assert set(params) == set(GS.GRAPH_SOURCES), params
                self.rows = [dict(r) for r in TABLES[table]]
                return
        for table, (prefix, scoped) in GS.SQL_BY_IDS.items():
            if sql.startswith(prefix):
                n = len(GS.GRAPH_SOURCES) if scoped else 0
                if scoped:
                    assert params[:n] == tuple(GS.GRAPH_SOURCES), params
                ids = set(params[n:])
                self.rows = [dict(r) for r in TABLES[table] if r[BY_ID_KEY[table]] in ids
                             and (not scoped or r["data_source"] in GS.GRAPH_SOURCES)]
                return
        raise AssertionError("TEST: unexpected SQL: " + " ".join(sql.split())[:120])

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        out, self.rows = self.rows, []
        return out

    def close(self):
        pass


CONNECTIONS = []
FK_ROWS = {"rows": kcu_rows(SCHEMA_FKS)}


def fake_get_db_connection():
    conn = FakeMariaDB(FK_ROWS["rows"])
    CONNECTIONS.append(conn)
    return conn


GS.get_db_connection = fake_get_db_connection


# ---- 5. In-memory Neo4j stand-in ------------------------------------------------------------
class Counters:
    def __init__(self):
        self.nodes_created = 0
        self.relationships_created = 0


class FakeGraph:
    WRITE_OPS = {"merge_nodes", "merge_rels", "delete_stale_rels", "delete_stale_nodes", "info_lock",
                 "info_record_constraints", "info_finish", "wipe_delete_rels", "wipe_delete_nodes", "wipe_delete_info"}
    SCHEMA_OPS = {"create_constraint", "drop_constraint"}

    def __init__(self):
        self.nodes, self.rels, self.constraints, self.nid, self.statements = {}, {}, {}, 0, []

    def state(self):
        return json.dumps([sorted((k, sorted(v["labels"]), sorted(v["props"].items(), key=str)) for k, v in self.nodes.items()),
                           sorted((k, v["type"], v["a"], v["b"], sorted(v["props"].items())) for k, v in self.rels.items()),
                           sorted(self.constraints)], default=str)

    def add_node(self, labels, props):
        self.nid += 1
        self.nodes[self.nid] = {"labels": set(labels), "props": dict(props)}
        return self.nid

    def add_rel(self, t, a, b):
        self.nid += 1
        self.rels[self.nid] = {"type": t, "a": a, "b": b, "props": {}}
        return self.nid

    def find(self, label, key, val):
        return [i for i, n in self.nodes.items() if label in n["labels"] and n["props"].get(key) == val]

    def attached(self, i):
        return [r for r in self.rels.values() if i in (r["a"], r["b"])]

    def exec(self, stmt, p, mode):
        used = set(re.findall(r"\$(\w+)", stmt.text))
        assert used <= set(p), (stmt.op, used - set(p))
        if mode == "read":
            assert stmt.op not in self.WRITE_OPS | self.SCHEMA_OPS, stmt.op
        self.statements.append(stmt.text)
        c, m, op, O = Counters(), stmt.meta, stmt.op, GS.OWNER
        own = lambda x: x["props"].get("syncOwner") == O  # noqa: E731
        if op == "foreign_nodes":
            return [{"c": sum(1 for n in self.nodes.values() if m["label"] in n["labels"] and not own(n))}], c
        if op == "foreign_rels":
            return [{"c": sum(1 for r in self.rels.values() if r["type"] == m["type"] and not own(r))}], c
        if op in ("info_lock", "info_record_constraints"):
            ids = self.find(GS.INFO_LABEL, "syncKey", p["infoKey"])
            if not ids:
                ids = [self.add_node({GS.INFO_LABEL}, {"syncKey": p["infoKey"], "syncOwner": p["owner"]})]
            n = self.nodes[ids[0]]
            if op == "info_lock":
                n["props"]["lockedByRun"] = p["runId"]
                return [{"owner": n["props"].get("syncOwner")}], c
            n["props"]["createdConstraints"] = [x for x in n["props"].get("createdConstraints", []) if x not in p["names"]] + list(p["names"])
            return [{"names": n["props"]["createdConstraints"]}], c
        if op == "info_finish":
            for i in self.find(GS.INFO_LABEL, "syncKey", p["infoKey"]):
                self.nodes[i]["props"].update(lastRunId=p["runId"], lastSyncedAt=p["syncedAt"], lastSummary=p["summary"])
                self.nodes[i]["props"].pop("lockedByRun", None)
            return [{"c": 1}], c
        if op == "info_read":
            return [{"infoKey": n["props"].get("syncKey"), "owner": n["props"].get("syncOwner"),
                     "createdConstraints": n["props"].get("createdConstraints"), "lastRunId": n["props"].get("lastRunId"),
                     "lastSyncedAt": n["props"].get("lastSyncedAt")} for n in self.nodes.values() if GS.INFO_LABEL in n["labels"]], c
        if op == "merge_nodes":
            for row in p["rows"]:
                ids = self.find(m["label"], m["key"], row["key"])
                if not ids:
                    ids = [self.add_node({m["label"]}, {m["key"]: row["key"]})]
                    c.nodes_created += 1
                n = self.nodes[ids[0]]
                for k, v in row["props"].items():
                    if v is None:
                        n["props"].pop(k, None)
                    else:
                        assert isinstance(v, (str, int, float, bool, date)) and not isinstance(v, Decimal), (k, type(v))
                        n["props"][k] = v
                n["labels"].add(GS.OWNER_LABEL)
                n["props"].update(syncOwner=p["owner"], syncRunId=p["runId"])
            return [{"n": len(p["rows"])}], c
        if op == "merge_rels":
            k = 0
            for row in p["rows"]:
                for ai in self.find(m["from"], m["fromKey"], row["src"]):
                    for bi in self.find(m["to"], m["toKey"], row["dst"]):
                        ex = [i for i, r in self.rels.items() if r["type"] == m["type"] and r["a"] == ai and r["b"] == bi]
                        if not ex:
                            ex = [self.add_rel(m["type"], ai, bi)]
                            c.relationships_created += 1
                        r = self.rels[ex[0]]
                        for kk, v in row["props"].items():
                            if v is None:
                                r["props"].pop(kk, None)
                            else:
                                r["props"][kk] = v
                        r["props"].update(syncOwner=p["owner"], syncRunId=p["runId"])
                        k += 1
            return [{"n": k}], c
        if op == "delete_stale_rels":
            ids = [i for i, r in self.rels.items() if r["type"] == m["type"] and own(r) and r["props"].get("syncRunId") != p["runId"]]
            for i in ids:
                del self.rels[i]
            return [{"removed": len(ids)}], c
        stale = [i for i, n in self.nodes.items() if "label" in m and m["label"] in n["labels"] and own(n)
                 and n["props"].get("syncRunId") != p.get("runId")]
        if op == "stale_attached":
            return [{"c": sum(1 for i in stale if self.attached(i))}], c
        if op == "delete_stale_nodes":
            for i in stale:
                if self.attached(i):
                    raise ClientError("stand-in: cannot delete a node that still has relationships")
                del self.nodes[i]
            return [{"removed": len(stale)}], c
        if op == "read_nodes":
            return [{"nodeKey": n["props"].get(m["key"]), "props": dict(n["props"])}
                    for n in self.nodes.values() if m["label"] in n["labels"] and own(n)], c
        if op == "read_rels":
            ka, kb = GS.NODE_KEYS[m["from"]], GS.NODE_KEYS[m["to"]]
            return [{"srcLabels": sorted(self.nodes[r["a"]]["labels"]), "srcKey": self.nodes[r["a"]]["props"].get(ka),
                     "dstLabels": sorted(self.nodes[r["b"]]["labels"]), "dstKey": self.nodes[r["b"]]["props"].get(kb),
                     "props": dict(r["props"])} for r in self.rels.values() if r["type"] == m["type"] and own(r)], c
        if op == "show_constraints":
            return [dict(v) for v in self.constraints.values()], c
        if op == "create_constraint":
            same = [v for v in self.constraints.values() if v["labelsOrTypes"] == [m["label"]] and v["properties"] == [m["prop"]]]
            if m["name"] not in self.constraints and not same:
                self.constraints[m["name"]] = {"name": m["name"], "type": "NODE_PROPERTY_UNIQUENESS", "entityType": "NODE",
                                               "labelsOrTypes": [m["label"]], "properties": [m["prop"]]}
            return [], c
        raise AssertionError("statement not understood by the stand-in: " + op)


class FakeTx:
    def __init__(self, g, mode):
        self.g, self.mode = g, mode


class FakeSession:
    def __init__(self, g):
        self.g = g

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _tx(self, fn, mode):
        saved = copy.deepcopy((self.g.nodes, self.g.rels, self.g.nid))
        try:
            return fn(FakeTx(self.g, mode))
        except BaseException:
            self.g.nodes, self.g.rels, self.g.nid = saved
            raise

    def execute_write(self, fn):
        return self._tx(fn, "write")

    def execute_read(self, fn):
        return self._tx(fn, "read")


class FakeDriver:
    def __init__(self, g):
        self.g = g

    def session(self, database=None):
        return FakeSession(self.g)


G = FakeGraph()
DRIVER_REQUESTS = []


def fake_get_driver(settings):
    DRIVER_REQUESTS.append(settings.database)
    return FakeDriver(G)


neo4j_db.get_driver = fake_get_driver
GS._run = lambda tx, stmt, params: tx.g.exec(stmt, params, tx.mode)
GS._run_auto = lambda session, stmt, params: session.g.exec(stmt, params, "auto")

# ---- 6. Expected graph for the fixed data above, written out by hand ----------------------
EXPECTED_NODES = {
    "Procurement": [101, 102, 103], "ProcuringEntity": [1], "Category": [5, 6], "ProcurementMode": [1, 2],
    "BidNotice": [201, 202, 203], "Supplier": [301, 302, 303], "Bid": [501, 502, 503, 504],
    "BacResolution": [601, 602, 603], "AwardNotice": [701, 702], "Contract": [801], "Person": [401, 402, 403],
}
EXPECTED_RELS = {
    "PROCURED_BY": [(101, 1), (102, 1), (103, 1)],
    "IN_CATEGORY": [(101, 5), (103, 6)],
    "USES_MODE": [(101, 1), (102, 2), (103, 1)],
    "NOTICE_FOR": [(201, 101), (202, 102), (203, 103)],
    "SUBMITTED": [(301, 501), (301, 504), (302, 502), (303, 503)],
    "FOR_PROCUREMENT": [(501, 101), (502, 101), (503, 102), (504, 103)],
    "UNDER_NOTICE": [(501, 201), (502, 201), (503, 202), (504, 203)],
    "RESOLVES": [(601, 101), (602, 102), (603, 103)],
    "RECOMMENDS": [(601, 501)],
    "AWARDS": [(701, 101), (702, 103)],
    "AWARDED_TO": [(701, 301), (702, 301)],
    "BASED_ON_BID": [(701, 501)],
    "BASED_ON_RESOLUTION": [(701, 601)],
    "UNDER_CONTRACT": [(701, 801)],
    "CERTIFIED_BY": [(701, 403)],
    "REPRESENTED_BY": [(301, 401), (302, 402)],
}

RES = []


def check(name, cond, extra=""):
    RES.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({extra})" if extra != "" else ""))


def keys(label):
    k = GS.NODE_KEYS[label]
    return sorted(n["props"][k] for n in G.nodes.values() if label in n["labels"])


def pairs(t):
    a, b, _ = GS.REL_TYPES[t]
    return sorted((G.nodes[r["a"]]["props"][GS.NODE_KEYS[a]], G.nodes[r["b"]]["props"][GS.NODE_KEYS[b]])
                  for r in G.rels.values() if r["type"] == t)


def graph_is_expected():
    bad = [l for l in EXPECTED_NODES if keys(l) != EXPECTED_NODES[l]]
    bad += [t for t in EXPECTED_RELS if pairs(t) != EXPECTED_RELS[t]]
    bad += sorted({r["type"] for r in G.rels.values()} - set(EXPECTED_RELS))
    return not bad, bad


def problems_for(body):
    return {p["constraint"]: p for p in body.get("foreignKeyProblems", [])}


print("graph_sync isolated tests - SIMULATED MariaDB (fixed result sets) and SIMULATED Neo4j (in memory)")
print("Python", sys.version.split()[0], "| neo4j package", neo4j_db.neo4j.__version__)

print("=== A. Definitions: 16 relationship types, 17 grounding constraints, 4 composite ===")
check("16 relationship types, unchanged", sorted(GS.REL_TYPES) == sorted(EXPECTED_RELS), sorted(GS.REL_TYPES))
req = {fk.name: fk for fk in GS.required_foreign_keys()}
check("17 required constraints = 13 single-column + 4 composite", sorted(req) == sorted(REQUIRED_SINGLE + COMPOSITES))
for name in COMPOSITES:
    n, t, cols, rt, rcols = get(name)
    check(f"{name} defined as {t} {tuple(cols)} -> {rt} {tuple(rcols)}",
          req[name].table == t and list(req[name].columns) == cols and req[name].ref_table == rt and list(req[name].ref_columns) == rcols)
check("metadata query reads CONSTRAINT_NAME and ORDINAL_POSITION, same schema, ordered",
      all(x in GS.SQL_FOREIGN_KEYS for x in ("CONSTRAINT_NAME", "ORDINAL_POSITION", "REFERENCED_TABLE_SCHEMA = DATABASE()",
                                              "ORDER BY TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION")))
check("composites ground UNDER_NOTICE, RECOMMENDS, BASED_ON_BID, BASED_ON_RESOLUTION",
      GS.REL_TYPES["UNDER_NOTICE"][2] == ["fk_bids_notice_same_proc"] and GS.REL_TYPES["RECOMMENDS"][2] == ["fk_res_bid_same_proc"]
      and GS.REL_TYPES["BASED_ON_BID"][2] == ["fk_awards_bid_same_proc_supplier"]
      and GS.REL_TYPES["BASED_ON_RESOLUTION"][2] == ["fk_awards_resolution_same_proc"])

print("=== B. check_foreign_keys (pure function) ===")
matched, probs = GS.check_foreign_keys(kcu_rows(SCHEMA_FKS))
check("all four composite constraints present -> no problems, 17 matched", not probs and len(matched) == 17, probs)
shuffled = list(reversed(kcu_rows(SCHEMA_FKS)))
check("row order from the server does not matter (grouped by name, ordered by ORDINAL_POSITION)",
      GS.check_foreign_keys(shuffled) == (matched, []))
renamed = replace("fk_bids_supplier", ("bids_ibfk_3", "bids", ["supplier_id"], "suppliers", ["supplier_id"]))
m2, p2 = GS.check_foreign_keys(kcu_rows(renamed))
check("same mapping under another constraint name is accepted and reported", not p2 and m2["fk_bids_supplier"] == "bids_ibfk_3")
for name in COMPOSITES:
    _, p = GS.check_foreign_keys(kcu_rows(without(name)))
    check(f"{name} missing -> reported as missing", list(problems_for({"foreignKeyProblems": p})) == [name]
          and p[0]["problem"] == "missing", p)
    _, p = GS.check_foreign_keys(kcu_rows(split(name)))
    check(f"{name} split into single-column constraints -> 'split across several constraints'",
          [x["constraint"] for x in p] == [name] and p[0]["problem"] == "split across several constraints"
          and len(p[0]["found"]) == len(get(name)[2]), p)
    _, p = GS.check_foreign_keys(kcu_rows(drop_last_column(name)))
    check(f"{name} missing a component column -> 'different columns'",
          [x["constraint"] for x in p] == [name] and p[0]["problem"] == "different columns or referenced table", p)
    _, p = GS.check_foreign_keys(kcu_rows(reversed_order(name)))
    check(f"{name} with the same pairs in another order -> 'columns in a different order'",
          [x["constraint"] for x in p] == [name] and p[0]["problem"] == "columns in a different order", p)
    _, p = GS.check_foreign_keys(kcu_rows(crossed(name)))
    check(f"{name} with crossed column mapping -> rejected", [x["constraint"] for x in p] == [name], p)
    _, p = GS.check_foreign_keys(kcu_rows(SCHEMA_FKS, positions={name: [1, 3, 4][:len(get(name)[2])]}))
    check(f"{name} with broken ORDINAL_POSITION sequence -> rejected", [x["constraint"] for x in p] == [name], p)
_, p = GS.check_foreign_keys(kcu_rows(wrong_ref_table("fk_res_bid_same_proc")))
check("composite pointing at the wrong table -> rejected", [x["constraint"] for x in p] == ["fk_res_bid_same_proc"], p)
extra_col = replace("fk_res_bid_same_proc", ("fk_res_bid_same_proc", "bac_resolutions",
                    ["recommended_bid_id", "procurement_id", "logged_by"], "bids", ["bid_id", "procurement_id", "recorded_by"]))
_, p = GS.check_foreign_keys(kcu_rows(extra_col))
check("composite with an extra column -> rejected", [x["constraint"] for x in p] == ["fk_res_bid_same_proc"], p)
for name in REQUIRED_SINGLE:
    _, p = GS.check_foreign_keys(kcu_rows(without(name)))
    check(f"single-column {name} missing -> reported", [x["constraint"] for x in p] == [name] and p[0]["problem"] == "missing", p)
_, p = GS.check_foreign_keys(kcu_rows([f for f in SCHEMA_FKS if f[0] not in ("fk_bids_supplier", "fk_awards_resolution_same_proc")]))
check("several missing -> all reported", sorted(x["constraint"] for x in p) == ["fk_awards_resolution_same_proc", "fk_bids_supplier"])
unused = [f for f in SCHEMA_FKS if f[3] != "users"]
check("constraints the graph does not use (to users) are not required", GS.check_foreign_keys(kcu_rows(unused))[1] == [])

print("=== C. First sync with every constraint present (simulated) ===")
status, body = GS.run_sync()
check("200 success, read-back verification ok", status == 200 and body["status"] == "success" and body["verification"]["ok"],
      (status, body.get("message"), body.get("foreignKeyProblems")))
ok, bad = graph_is_expected()
check("nodes and all 16 relationship types equal the hand-written expectation", ok, bad)
check("summary reports 17 constraints checked and the actual names matched",
      body["source"]["foreignKeysChecked"] == 17 and body["source"]["foreignKeyConstraints"] == {n: n for n in req})
check("RECOMMENDS to an out-of-scope bid skipped, NULL foreign keys skipped",
      body["relationships"]["RECOMMENDS"] == {"rows": 1, "created": 1, "matched": 0, "skippedNullForeignKey": 1, "skippedOutOfScope": 1}
      and body["relationships"]["IN_CATEGORY"]["skippedNullForeignKey"] == 1, body["relationships"]["RECOMMENDS"])
cert = [r for r in G.rels.values() if r["type"] == "CERTIFIED_BY"]
rep = sorted((G.nodes[r["a"]]["props"]["supplierId"], r["props"].get("position")) for r in G.rels.values() if r["type"] == "REPRESENTED_BY")
check("relationship properties: CERTIFIED_BY.role and REPRESENTED_BY.position (NULL -> absent)",
      cert[0]["props"].get("role") == "BAC Chairperson" and rep == [(301, "Owner"), (302, None)], rep)
p102 = G.nodes[G.find("Procurement", "procurementId", 102)[0]]["props"]
check("values: DECIMAL -> float, NULL column -> property absent",
      p102["abc"] == 2500000.5 and "targetTimeline" not in p102 and p102["controlNo"] == "CN-7")
conn = CONNECTIONS[-1]
check("MariaDB: one READ ONLY consistent-snapshot transaction, rolled back, closed, SELECT only",
      conn.started == [{"consistent_snapshot": True, "readonly": True}] and conn.rollbacks >= 1 and conn.closed)
status, body = GS.run_sync()
check("repeat sync: 0 nodes and 0 relationships created, graph unchanged",
      status == 200 and body["totals"]["nodesCreated"] == 0 and body["totals"]["relationshipsCreated"] == 0
      and graph_is_expected()[0])
GOOD_STATE = G.state()          # snapshot of the synced graph before the refusal cases


def failing_case(label, fk_rows, expect_constraints, expect_problem=None):
    FK_ROWS["rows"] = fk_rows
    before_state, before_stmts, before_drivers = G.state(), len(G.statements), len(DRIVER_REQUESTS)
    status, body = GS.run_sync()
    conn = CONNECTIONS[-1]
    got = problems_for(body)
    ok = (status == 409 and body.get("status") == "refused" and body.get("stage") == "read_mariadb"
          and sorted(got) == sorted(expect_constraints)
          and (expect_problem is None or all(got[c]["problem"] == expect_problem for c in expect_constraints)))
    check(f"{label} -> 409 before any Neo4j work", ok, (status, body.get("stage"), {c: got[c]["problem"] for c in got}))
    check("   ... Neo4j driver never requested, no Cypher sent, graph unchanged",
          len(DRIVER_REQUESTS) == before_drivers and len(G.statements) == before_stmts and G.state() == before_state)
    check("   ... MariaDB: only DATABASE() and the constraint query ran; transaction rolled back",
          len(conn.queries) == 2 and conn.queries[1] == GS.SQL_FOREIGN_KEYS and conn.rollbacks >= 1 and conn.closed)
    FK_ROWS["rows"] = kcu_rows(SCHEMA_FKS)


print("=== D. Validation failures stop the sync before Neo4j (graph from C already present) ===")
for name in COMPOSITES:
    failing_case(f"{name} missing", kcu_rows(without(name)), [name], "missing")
    failing_case(f"{name} incorrectly grouped (split)", kcu_rows(split(name)), [name], "split across several constraints")
    failing_case(f"{name} columns reordered", kcu_rows(reversed_order(name)), [name], "columns in a different order")
for name in REQUIRED_SINGLE:
    failing_case(f"{name} missing", kcu_rows(without(name)), [name], "missing")
check("existing graph relationships still exactly as expected after all refusals", graph_is_expected()[0] and G.state() == GOOD_STATE)

print("=== E. --verify path refuses the same way and never writes ===")
FK_ROWS["rows"] = kcu_rows(without("fk_awards_resolution_same_proc"))
before = (G.state(), len(G.statements), len(DRIVER_REQUESTS))
status, body = GS.run_verify()
check("run_verify with a missing composite -> 409 naming it, before Neo4j",
      status == 409 and list(problems_for(body)) == ["fk_awards_resolution_same_proc"]
      and before == (G.state(), len(G.statements), len(DRIVER_REQUESTS)))
FK_ROWS["rows"] = kcu_rows(SCHEMA_FKS)
check("python graph_sync.py --verify (main) -> exit 0 when all constraints present", GS.main(["--verify"]) == 0)
FK_ROWS["rows"] = kcu_rows(split("fk_bids_notice_same_proc"))
check("python graph_sync.py --verify (main) -> exit 1 when a composite is split", GS.main(["--verify"]) == 1)
FK_ROWS["rows"] = kcu_rows(SCHEMA_FKS)

print("=== F. After restoring the constraints, sync works again and nothing drifted ===")
status, body = GS.run_sync()
check("sync succeeds, 0 created, graph equals expectation", status == 200 and body["totals"]["nodesCreated"] == 0
      and body["totals"]["relationshipsCreated"] == 0 and graph_is_expected()[0])

print("=== G. Test safety ===")
check("real MariaDB connector never called", not REAL_DB_ATTEMPTS)
check("real Neo4j driver never created", not REAL_DRIVER_ATTEMPTS)
check("no MariaDB commit, no non-SELECT statement (enforced by the stand-in)", all(c.closed for c in CONNECTIONS))

passed = sum(ok for _, ok in RES)
print(f"\nTOTAL: {passed}/{len(RES)} passed  (SIMULATED - no real MariaDB or Neo4j was used)")
sys.exit(0 if passed == len(RES) else 1)
