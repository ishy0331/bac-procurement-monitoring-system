"""
backend/t_graph_store.py - ISOLATED, SIMULATED tests for graph_store.py (GET /api/graph/neo4j reader).

Run from backend/:   python t_graph_store.py

Safety (no real database is touched):
  * a stand-in module is installed as "db" before anything imports it (real MariaDB unreachable);
  * neo4j_db.GraphDatabase is replaced so no real driver can be created; NEO4J_* are dummy values
    in this process only;
  * Neo4j is replaced by FakeReadDriver: sessions record their access mode, execute_write fails the
    test, and only the three fixed read statements of graph_store are understood (any other text
    fails the test). The stand-in applies the same ownership rules the Cypher WHERE clauses express.
Real: graph_store.py, graph_sync.py constants, neo4j_db.py, the neo4j package (exceptions, Date type).
"""
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
if "db" in sys.modules:
    sys.exit("TEST SAFETY: the real db module is already imported; run this file on its own.")
REAL_DB = []


def _forbidden_db(*_a, **_k):
    REAL_DB.append(1)
    raise AssertionError("TEST SAFETY: a real MariaDB connection was attempted")


_fake_db = types.ModuleType("db")
_fake_db.get_db_connection = _forbidden_db
sys.modules["db"] = _fake_db
DUMMY_PASSWORD = "test-password-not-real-7731"
os.environ.update(NEO4J_URI="bolt://127.0.0.1:9", NEO4J_USERNAME="test-user-not-real",
                  NEO4J_PASSWORD=DUMMY_PASSWORD, NEO4J_DATABASE="neo4j")

import neo4j_db  # noqa: E402

if neo4j_db.neo4j is None:
    sys.exit('The neo4j driver package is required for these tests. Install it with: pip install "neo4j==6.4.1"')
REAL_DRIVER = []


class _ForbiddenGraphDatabase:
    @staticmethod
    def driver(*_a, **_k):
        REAL_DRIVER.append(1)
        raise AssertionError("TEST SAFETY: a real Neo4j driver was requested")


neo4j_db.GraphDatabase = _ForbiddenGraphDatabase

import graph_store as GSt  # noqa: E402
import graph_sync as GS  # noqa: E402
from neo4j.exceptions import AuthError, ClientError, ServiceUnavailable  # noqa: E402
from neo4j.time import Date  # noqa: E402

O, ML = GS.OWNER, GS.OWNER_LABEL
SYNC_TIME = "2026-11-06T09:12:30+00:00"


def owned(eid, label, props, extra_labels=()):
    return {"eid": eid, "labels": [label, ML, *extra_labels], "props": dict(props, syncOwner=O, syncRunId="run-1")}


def info(**over):
    row = {"syncKey": GS.INFO_KEY, "syncOwner": O, "lastRunId": "run-1", "lastSyncedAt": SYNC_TIME,
           "lastSummary": json.dumps({"totals": {"nodesCreated": 0, "relationshipsCreated": 0}, "verificationOk": True})}
    row.update(over)
    return row


def base_graph():
    """A small owned graph with all 11 labels and all 16 relationship types, plus foreign data."""
    N = [
        owned("e1", "Procurement", {"procurementId": 101, "referenceNo": "SR-2026-101", "title": "Office supplies",
                                    "abc": 150000.0, "status": "Awarded", "dataSource": "sample", "fiscalYear": 2026}),
        owned("e2", "ProcuringEntity", {"entityId": 1, "name": "Sample City Government"}),
        owned("e3", "Category", {"categoryId": 5, "name": "Office Supplies"}),
        owned("e4", "ProcurementMode", {"modeId": 1, "name": "Small Value Procurement", "isCompetitive": False}),
        owned("e5", "BidNotice", {"noticeId": 201, "noticeType": "RFQ", "submissionDeadline": Date(2026, 9, 15)}),
        owned("e6", "Supplier", {"supplierId": 301, "name": "Alpha Trading", "dataSource": "sample"}),
        owned("e7", "Bid", {"bidId": 501, "amount": 140000.0, "rankNo": 1}),
        owned("e8", "BacResolution", {"resolutionId": 601, "resolutionNo": "R-601", "recommendation": "LCRB",
                                      "postQualificationDone": True, "resolutionDate": Date(2026, 9, 20)}),
        owned("e9", "AwardNotice", {"awardId": 701, "noticeNo": "NOA-701", "awardedAmount": 140000.0,
                                    "awardDate": Date(2026, 9, 25)}),
        owned("e10", "Contract", {"contractId": 801, "contractNo": "PO-801"}),
        owned("e11", "Person", {"personId": 401, "fullName": "Ana Cruz"}),
        owned("e12", "Person", {"personId": 403, "fullName": "Carla Santos"}),
        # foreign data that must never be returned
        {"eid": "f1", "labels": ["Movie"], "props": {"title": "The Matrix"}},
        {"eid": "f2", "labels": ["Person"], "props": {"name": "Keanu Reeves"}},
        {"eid": "f3", "labels": [ML, "Supplier"], "props": {"supplierId": 999, "syncOwner": "someone-else"}},
    ]
    R = [(t, a, b, p) for t, a, b, p in (
        ("PROCURED_BY", "e1", "e2", {}), ("IN_CATEGORY", "e1", "e3", {}), ("USES_MODE", "e1", "e4", {}),
        ("NOTICE_FOR", "e5", "e1", {}), ("SUBMITTED", "e6", "e7", {}), ("FOR_PROCUREMENT", "e7", "e1", {}),
        ("UNDER_NOTICE", "e7", "e5", {}), ("RESOLVES", "e8", "e1", {}), ("RECOMMENDS", "e8", "e7", {}),
        ("AWARDS", "e9", "e1", {}), ("AWARDED_TO", "e9", "e6", {}), ("BASED_ON_BID", "e9", "e7", {}),
        ("BASED_ON_RESOLUTION", "e9", "e8", {}), ("UNDER_CONTRACT", "e9", "e10", {}),
        ("CERTIFIED_BY", "e9", "e12", {"role": "BAC Chairperson"}), ("REPRESENTED_BY", "e6", "e11", {"position": "Owner"}))]
    rels = [{"type": t, "src": a, "dst": b, "props": dict(p, syncOwner=O, syncRunId="run-1")} for t, a, b, p in R]
    rels += [
        {"type": "ACTED_IN", "src": "f2", "dst": "f1", "props": {}},                         # foreign
        {"type": "LIKES", "src": "f2", "dst": "e6", "props": {}},                            # foreign rel on owned node
        {"type": "AWARDS", "src": "e9", "dst": "f1", "props": {"syncOwner": O}},              # owned rel to foreign node
        {"type": "AWARDED_TO", "src": "e9", "dst": "f3", "props": {"syncOwner": O}},          # owned rel to other owner's node
    ]
    return {"info": [info()], "nodes": N, "rels": rels}


class FakeReadTx:
    def __init__(self, store):
        self.store = store


class FakeReadSession:
    def __init__(self, store, mode):
        self.store, self.mode = store, mode

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute_read(self, fn):
        self.store.reads += 1
        if self.store.fail:
            raise self.store.fail
        return fn(FakeReadTx(self.store))

    def execute_write(self, fn):
        self.store.writes += 1
        raise AssertionError("TEST SAFETY: graph_store opened a write transaction")

    def run(self, *a, **k):
        raise AssertionError("TEST SAFETY: graph_store used an auto-commit statement")


class FakeStore:
    def __init__(self, data):
        self.data, self.fail, self.reads, self.writes = data, None, 0, 0
        self.modes, self.texts, self.databases = [], [], []

    def owned_node(self, n):
        return ML in n["labels"] and n["props"].get("syncOwner") == O

    def query(self, text, params):
        if text not in GSt.READ_QUERIES:
            raise AssertionError("TEST: unexpected statement " + text[:80])
        assert params == {"owner": O, "infoKey": GS.INFO_KEY}, params
        self.texts.append(text)
        if text == GSt.READ_INFO:
            return [{"lastRunId": r.get("lastRunId"), "lastSyncedAt": r.get("lastSyncedAt"), "lastSummary": r.get("lastSummary")}
                    for r in self.data["info"] if r.get("syncKey") == params["infoKey"] and r.get("syncOwner") == params["owner"]]
        if text == GSt.READ_NODES:
            return [{"eid": n["eid"], "labels": list(n["labels"]), "props": dict(n["props"])} for n in self.data["nodes"] if self.owned_node(n)]
        byid = {n["eid"]: n for n in self.data["nodes"]}
        return [{"relType": r["type"], "srcEid": r["src"], "dstEid": r["dst"], "props": dict(r["props"])}
                for r in self.data["rels"] if r["props"].get("syncOwner") == O
                and r["src"] in byid and r["dst"] in byid and self.owned_node(byid[r["src"]]) and self.owned_node(byid[r["dst"]])]


class FakeReadDriver:
    def __init__(self, store):
        self.store = store

    def session(self, database=None, default_access_mode=None):
        self.store.modes.append(default_access_mode)
        self.store.databases.append(database)
        return FakeReadSession(self.store, default_access_mode)


STORE = {"s": FakeStore(base_graph())}
neo4j_db.get_driver = lambda settings: FakeReadDriver(STORE["s"])
GSt._run = lambda tx, text, params: tx.store.query(text, params)
RES = []
BODIES = []


def check(name, cond, extra=""):
    RES.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({extra})" if extra != "" else ""))


def read(store=None):
    if store is not None:
        STORE["s"] = store
    status, body = GSt.read_graph()
    BODIES.append(json.dumps(body))
    return status, body


print("graph_store isolated tests - SIMULATED Neo4j (in-memory read-only stand-in); no real MariaDB or Neo4j")

print("=== A. Fixed queries, READ transactions ===")
check("exactly three fixed, application-controlled read statements (module constants)", len(GSt.READ_QUERIES) == 3
      and GSt.READ_QUERIES == (GSt.READ_INFO, GSt.READ_NODES, GSt.READ_RELS))
check("values are parameters ($owner, $infoKey); no keyword guard is used as a security mechanism",
      all("$owner" in q for q in GSt.READ_QUERIES) and not hasattr(GSt, "_WRITE_WORDS"))
check("ownership markers are the ones graph_sync writes", GSt.READ_NODES.count(f"`{GS.OWNER_LABEL}`") == 1 and
      "syncOwner = $owner" in GSt.READ_RELS and f"`{GS.INFO_LABEL}`" in GSt.READ_INFO)

print("=== B. Normal synced graph ===")
status, body = read()
s = STORE["s"]
check("200 success, source neo4j, snapshot true", status == 200 and body["status"] == "success" and body["source"] == "neo4j" and body["snapshot"] is True)
check("session opened in READ access mode, only execute_read, never a write", s.modes == [neo4j_db.neo4j.READ_ACCESS] and s.writes == 0 and s.reads == 1)
check("only the three read statements were sent", sorted(s.texts) == sorted(GSt.READ_QUERIES))
check("12 owned nodes returned, foreign nodes (Movie, unowned Person, other owner) excluded", body["counts"]["nodes"] == 12
      and not any(n["label"] == "Movie" for n in body["nodes"]) and not any(n["properties"].get("name") == "Keanu Reeves" for n in body["nodes"])
      and "Supplier:999" not in [n["id"] for n in body["nodes"]])
check("16 relationships, one per type; foreign and dangling relationships excluded",
      body["counts"]["relationships"] == 16 and sorted(r["type"] for r in body["relationships"]) == sorted(GS.REL_TYPES))
ids = {n["id"] for n in body["nodes"]}
check("stable IDs from the MariaDB key: Procurement:101, Supplier:301, Bid:501 ...",
      {"Procurement:101", "ProcuringEntity:1", "Category:5", "ProcurementMode:1", "BidNotice:201", "Supplier:301", "Bid:501",
       "BacResolution:601", "AwardNotice:701", "Contract:801", "Person:401", "Person:403"} == ids)
rel = {r["type"]: r for r in body["relationships"]}
check("exact types and directions preserved (e.g. AWARDS AwardNotice->Procurement, SUBMITTED Supplier->Bid)",
      rel["AWARDS"]["source"] == "AwardNotice:701" and rel["AWARDS"]["target"] == "Procurement:101"
      and rel["SUBMITTED"]["source"] == "Supplier:301" and rel["SUBMITTED"]["target"] == "Bid:501"
      and rel["NOTICE_FOR"]["source"] == "BidNotice:201" and rel["UNDER_NOTICE"]["target"] == "BidNotice:201")
check("relationship IDs come from the source record's primary key", rel["AWARDS"]["id"] == "AWARDS:award_notices:701"
      and rel["SUBMITTED"]["id"] == "SUBMITTED:bids:501" and rel["NOTICE_FOR"]["id"] == "NOTICE_FOR:bid_notices:201"
      and rel["RECOMMENDS"]["id"] == "RECOMMENDS:bac_resolutions:601" and rel["PROCURED_BY"]["id"] == "PROCURED_BY:procurements:101"
      and rel["REPRESENTED_BY"]["id"] == "REPRESENTED_BY:supplier_representatives:301,401", {t: r["id"] for t, r in rel.items()})
check("all 16 relationship IDs stable and unique", all(r["stableId"] for r in body["relationships"])
      and len({r["id"] for r in body["relationships"]}) == 16 and body["counts"]["unmappedRelationships"] == 0)
check("relationship properties kept (role, position); markers removed",
      rel["CERTIFIED_BY"]["properties"] == {"role": "BAC Chairperson"} and rel["REPRESENTED_BY"]["properties"] == {"position": "Owner"}
      and all("syncOwner" not in r["properties"] for r in body["relationships"]))
p101 = next(n for n in body["nodes"] if n["id"] == "Procurement:101")
check("node: label, labels (marker label removed), properties (markers removed)", p101["label"] == "Procurement"
      and p101["labels"] == ["Procurement"] and "syncOwner" not in p101["properties"] and "syncRunId" not in p101["properties"]
      and p101["properties"]["title"] == "Office supplies" and p101["known"] and p101["stableId"])
an = next(n for n in body["nodes"] if n["id"] == "AwardNotice:701")
check("Neo4j Date values returned as ISO strings", an["properties"]["awardDate"] == "2026-09-25")
check("sync metadata: last sync time, run id, verification, totals", body["sync"] == {"lastRunId": "run-1", "lastSyncedAt": SYNC_TIME,
      "verificationOk": True, "totals": {"nodesCreated": 0, "relationshipsCreated": 0}})
check("labelled as synced sample snapshot, not live", body["dataLabel"] == "Synced City Sample Data" and "not live" in body["notice"]
      and body["dataSources"] == ["sample"] and body["database"] == "neo4j")
check("schema lists the 11 labels and 16 types", body["schema"] == {"nodeLabels": list(GS.NODE_KEYS), "relationshipTypes": list(GS.REL_TYPES)})
check("no unknown labels or types in a normal graph", body["counts"]["unmappedNodes"] == 0 and body["counts"]["unknownRelationshipTypes"] == [])
check("response is JSON-serialisable and deterministic", json.dumps(read()[1], sort_keys=True) == json.dumps(read()[1], sort_keys=True))

print("=== C. Unknown labels / relationships, missing properties ===")
g = base_graph()
g["nodes"].append(owned("e20", "Inspection", {"inspectionId": 1, "note": "unexpected label"}))
g["nodes"].append(owned("e21", "Supplier", {"name": "No key supplier"}))                           # key property missing
g["nodes"].append(owned("e22", "Procurement", {"procurementId": 102}))                               # most properties missing
g["rels"].append({"type": "INSPECTED", "src": "e20", "dst": "e1", "props": {"syncOwner": O}})
g["rels"].append({"type": "SUBMITTED", "src": "e21", "dst": "e7", "props": {"syncOwner": O}})
status, body = read(FakeStore(g))
unk = [n for n in body["nodes"] if n["label"] == "Inspection"]
check("unknown label returned (not discarded), known false, unmapped id", status == 200 and len(unk) == 1 and not unk[0]["known"]
      and unk[0]["id"].startswith("unmapped:") and unk[0]["properties"]["note"] == "unexpected label")
nokey = [n for n in body["nodes"] if n["properties"].get("name") == "No key supplier"]
check("node without its key property returned with stableId false", len(nokey) == 1 and not nokey[0]["stableId"] and nokey[0]["known"])
p102 = [n for n in body["nodes"] if n["id"] == "Procurement:102"]
check("node with missing properties returned as is (no invented values)", len(p102) == 1 and p102[0]["properties"] == {"procurementId": 102})
ins = [r for r in body["relationships"] if r["type"] == "INSPECTED"]
check("unknown relationship type returned, known false, listed in counts", len(ins) == 1 and not ins[0]["known"]
      and body["counts"]["unknownRelationshipTypes"] == ["INSPECTED"] and body["counts"]["unmappedNodes"] == 2)
check("relationship from the unmapped node points at its unmapped id", any(r["type"] == "SUBMITTED" and r["source"] == nokey[0]["id"] for r in body["relationships"]))
check("relationships without a provable source record get unmapped-rel ids, stableId false (unknown type; end node without key)",
      all(r["id"].startswith("unmapped-rel:") and not r["stableId"] for r in body["relationships"] if r["type"] == "INSPECTED" or r["source"] == nokey[0]["id"])
      and body["counts"]["unmappedRelationships"] == 2)
dangling = GSt.build_graph([], [owned("x1", "Bid", {"bidId": 1})],
                           [{"relType": "FOR_PROCUREMENT", "srcEid": "x1", "dstEid": "missing", "props": {}}])
check("build_graph drops a relationship whose end node was not returned (defence in depth)", dangling["relationships"] == [])
dup = GSt.build_graph([], [owned("d1", "Bid", {"bidId": 7}), owned("d2", "Bid", {"bidId": 7})], [])
check("two nodes claiming one key: both kept, second flagged unmapped", len(dup["nodes"]) == 2 and sum(n["stableId"] for n in dup["nodes"]) == 1)

print("=== C2. Relationship ID uniqueness ===")
mp = GSt.REL_SOURCE_RECORD
check("every one of the 16 types has a source table, matching graph_sync.REL_SOURCES", set(mp) == set(GS.REL_TYPES)
      and all(GS.REL_SOURCES[t][0] == mp[t][0] for t in mp))
# A larger synthetic graph shaped by the schema: per procurement 1 notice, 3 bids from 3 suppliers, 1 resolution, 1 award.
nodes, rels = [], []
def add(eid, label, key, **p):
    nodes.append(owned(eid, label, dict({GS.NODE_KEYS[label]: key}, **p)))
def link(t, a, b, **p):
    rels.append({"relType": t, "srcEid": a, "dstEid": b, "props": dict(p, syncOwner=O)})
add("ent", "ProcuringEntity", 1); add("cat", "Category", 1); add("mode", "ProcurementMode", 1); add("con", "Contract", 1)
for s_ in range(1, 4):
    add(f"s{s_}", "Supplier", s_); add(f"p{s_}", "Person", s_); link("REPRESENTED_BY", f"s{s_}", f"p{s_}")
add("p9", "Person", 9); link("REPRESENTED_BY", "s1", "p9"); link("REPRESENTED_BY", "s2", "p9")      # one person, two suppliers
for pr in range(1, 6):
    add(f"pr{pr}", "Procurement", pr, dataSource="sample"); link("PROCURED_BY", f"pr{pr}", "ent"); link("IN_CATEGORY", f"pr{pr}", "cat"); link("USES_MODE", f"pr{pr}", "mode")
    add(f"n{pr}", "BidNotice", pr); link("NOTICE_FOR", f"n{pr}", f"pr{pr}")
    for s_ in range(1, 4):
        b = pr * 10 + s_; add(f"b{b}", "Bid", b); link("SUBMITTED", f"s{s_}", f"b{b}"); link("FOR_PROCUREMENT", f"b{b}", f"pr{pr}"); link("UNDER_NOTICE", f"b{b}", f"n{pr}")
    add(f"r{pr}", "BacResolution", pr); link("RESOLVES", f"r{pr}", f"pr{pr}"); link("RECOMMENDS", f"r{pr}", f"b{pr * 10 + 1}")
    add(f"a{pr}", "AwardNotice", pr); link("AWARDS", f"a{pr}", f"pr{pr}"); link("AWARDED_TO", f"a{pr}", "s1"); link("BASED_ON_BID", f"a{pr}", f"b{pr * 10 + 1}")
    link("BASED_ON_RESOLUTION", f"a{pr}", f"r{pr}"); link("UNDER_CONTRACT", f"a{pr}", "con"); link("CERTIFIED_BY", f"a{pr}", "p9")
big = GSt.build_graph([info()], nodes, rels)
ids_ = [r["id"] for r in big["relationships"]]
check(f"{len(ids_)} relationships of all 16 types: every ID stable and unique", len(ids_) == len(set(ids_)) == len(rels)
      and all(r["stableId"] for r in big["relationships"]) and len({r["type"] for r in big["relationships"]}) == 16)
check("shared targets do not collide (5 awards -> one contract, one supplier, one certifier)",
      sorted(r["id"] for r in big["relationships"] if r["type"] == "UNDER_CONTRACT") == [f"UNDER_CONTRACT:award_notices:{i}" for i in range(1, 6)])
check("composite key keeps one person representing two suppliers apart",
      {"REPRESENTED_BY:supplier_representatives:1,9", "REPRESENTED_BY:supplier_representatives:2,9"} <= set(ids_))
check("IDs do not depend on query order", [r["id"] for r in GSt.build_graph([info()], list(reversed(nodes)), list(reversed(rels)))["relationships"]] == ids_)
wrong = GSt.build_graph([], [owned("w1", "Supplier", {"supplierId": 1}), owned("w2", "Procurement", {"procurementId": 1})],
                        [{"relType": "AWARDS", "srcEid": "w1", "dstEid": "w2", "props": {}}])
check("a known type between unexpected labels is not given a source-record ID", wrong["relationships"][0]["id"].startswith("unmapped-rel:")
      and not wrong["relationships"][0]["stableId"])
dupe = GSt.build_graph([], [owned("d1", "AwardNotice", {"awardId": 1}), owned("d2", "Procurement", {"procurementId": 1}),
                            owned("d3", "Procurement", {"procurementId": 2})],
                       [{"relType": "AWARDS", "srcEid": "d1", "dstEid": "d2", "props": {}}, {"relType": "AWARDS", "srcEid": "d1", "dstEid": "d3", "props": {}}])
check("two AWARDS from one award row (impossible from graph_sync) -> second flagged, counted", dupe["counts"]["duplicateRelationshipIds"] == 1
      and sum(r["stableId"] for r in dupe["relationships"]) == 1)

print("=== C3. Sync metadata is passed through as stored, never invented ===")
m = GSt.build_graph([info(lastSummary=json.dumps({"totals": {"nodesCreated": 3}, "verificationOk": False}))], [], [])["sync"]
check("stored verificationOk false is returned false", m["verificationOk"] is False and m["totals"] == {"nodesCreated": 3})
m = GSt.build_graph([info(lastSummary=json.dumps({"totals": {"nodesCreated": 3}}))], [], [])["sync"]
check("verificationOk not stored -> null (not assumed true)", m["verificationOk"] is None and m["totals"] == {"nodesCreated": 3})
m = GSt.build_graph([info(lastSummary=json.dumps({"verificationOk": "yes", "totals": [1]}))], [], [])["sync"]
check("wrong types in the stored summary -> null, not coerced", m["verificationOk"] is None and m["totals"] is None)
m = GSt.build_graph([info(lastSummary=None, lastSyncedAt=None, lastRunId=None)], [], [])["sync"]
check("missing stored properties -> nulls", m == {"lastRunId": None, "lastSyncedAt": None, "verificationOk": None, "totals": None})
check("READ_INFO reads exactly lastRunId, lastSyncedAt, lastSummary", all(f"i.{k} AS {k}" in GSt.READ_INFO for k in ("lastRunId", "lastSyncedAt", "lastSummary"))
      and GSt.READ_INFO.count(" AS ") == 3)
check("graph_sync writes exactly those three on its final step", all(f"i.{k} = $" in GS.INFO_FINISH.text for k in ("lastRunId", "lastSyncedAt", "lastSummary"))
      and '"verificationOk"' in open(os.path.join(HERE, "graph_sync.py")).read())

print("=== D. Empty graph, never synced, metadata missing, unexpected data ===")
status, body = read(FakeStore({"info": [info()], "nodes": [], "rels": []}))
check("synced but empty graph -> 200 with empty lists", status == 200 and body["nodes"] == [] and body["relationships"] == []
      and body["counts"]["nodes"] == 0 and body["sync"]["lastSyncedAt"] == SYNC_TIME)
status, body = read(FakeStore({"info": [], "nodes": [], "rels": []}))
check("never synchronized -> 404 not_synced", status == 404 and body["neo4j"] == "not_synced" and body["status"] == "error")
status, body = read(FakeStore({"info": [], "nodes": base_graph()["nodes"], "rels": base_graph()["rels"]}))
check("owned nodes but no sync record -> 200 with sync null", status == 200 and body["sync"] is None and body["counts"]["nodes"] == 12)
status, body = read(FakeStore({"info": [info(lastSummary="not json")], "nodes": [], "rels": []}))
check("unreadable summary -> metadata kept, verification unknown", status == 200 and body["sync"]["verificationOk"] is None and body["sync"]["totals"] is None)
foreign_info = FakeStore({"info": [info(syncOwner="someone-else")], "nodes": [], "rels": []})
status, body = read(foreign_info)
check("sync record of another owner is ignored -> 404", status == 404)
g = base_graph()
g["nodes"].append(owned("e30", "Supplier", {"supplierId": 302, "name": "Ref Co", "dataSource": "philgeps"}))
status, body = read(FakeStore(g))
check("unexpected data source (philgeps) -> 409, nothing shown", status == 409 and body["neo4j"] == "unexpected_data"
      and body["dataSources"] == ["philgeps"] and "nodes" not in body)

print("=== E. Neo4j unavailable / misconfigured -> 503, no fallback ===")
cases = [("server unreachable", ServiceUnavailable("stand-in: connection refused"), "unreachable"),
         ("credentials rejected", AuthError("stand-in: unauthorized"), "auth_failed"),
         ("other driver error", ClientError("stand-in: something odd with secret " + DUMMY_PASSWORD), "error")]
for label, exc, state in cases:
    st_ = FakeStore(base_graph()); st_.fail = exc
    status, body = read(st_)
    check(f"{label} -> 503 {state}, generic message", status == 503 and body["neo4j"] == state and body["source"] == "neo4j"
          and "nodes" not in body and DUMMY_PASSWORD not in json.dumps(body))
os.environ.pop("NEO4J_PASSWORD")
status, body = read(FakeStore(base_graph()))
check("NEO4J_PASSWORD missing -> 503 not_configured", status == 503 and body["neo4j"] == "not_configured")
os.environ["NEO4J_PASSWORD"] = DUMMY_PASSWORD
saved = neo4j_db.neo4j
neo4j_db.neo4j = None
status, body = read(FakeStore(base_graph()))
check("driver not installed -> 503 driver_missing", status == 503 and body["neo4j"] == "driver_missing")
neo4j_db.neo4j = saved

print("=== F. Safety ===")
allb = "\n".join(BODIES)
check("no password, user name or URI in any response", DUMMY_PASSWORD not in allb and "test-user-not-real" not in allb and "127.0.0.1" not in allb)
check("no marker properties or element IDs in any response", '"syncOwner"' not in allb and '"syncRunId"' not in allb and '"eid"' not in allb)
check("real MariaDB connector never called, real driver never created", not REAL_DB and not REAL_DRIVER)
check("graph_sync definitions unchanged (16 types, 11 labels)", len(GS.REL_TYPES) == 16 and len(GS.NODE_KEYS) == 11)

passed = sum(ok for _, ok in RES)
print(f"\nTOTAL: {passed}/{len(RES)} passed  (SIMULATED - no real MariaDB or Neo4j was used)")
sys.exit(0 if passed == len(RES) else 1)
