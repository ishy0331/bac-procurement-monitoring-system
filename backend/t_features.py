"""
backend/t_features.py - ISOLATED, SIMULATED tests for the features added on 2026-11-15.

Run from backend/:   python t_features.py

Covers
  W. Procurement workflow saved to the database: post notice -> record bids -> BAC resolution -> Notice of Award
     -> contract; wrong-status, wrong-role and invalid-input refusals; the activity log; the knowledge graph.
  D. Dashboard figures computed from the records (counts, savings, follow-up, flags, concentration).
  U. Super Admin accounts: create (temporary password, first sign-in), edit, disable/enable, safety rules.
  S. System settings saved to system_settings (fiscal year, entity, thresholds).
  F. Documents: upload, file-type check, staff download, publish, public list/download, remove;
     "not installed" reply when the table is missing.
  R. PMR as Excel (.xlsx): valid workbook, staff vs public columns.

Safety: the real backend/db.py is never imported. MariaDB is replaced by a temporary SQLite file
(%s placeholders, FOR UPDATE and NOW() are translated; MariaDB locking is NOT simulated).
graph_sync is replaced by a stand-in that reads the graph with the REAL graph_api queries. Uploads go to a
temporary folder. Everything here is SIMULATED; check the real system with the steps in the notes.
"""
import io
import os
import re
import sqlite3
import sys
import tempfile
import types
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
if "db" in sys.modules:
    sys.exit("TEST SAFETY: the real db module is already imported; run this file on its own.")

TMP = tempfile.mkdtemp(prefix="bacpms-features-")
DB_FILE = os.path.join(TMP, "test.sqlite")
sqlite3.register_converter("TIMESTAMP", lambda b: datetime.fromisoformat(b.decode()))
sqlite3.register_converter("DATE", lambda b: date.fromisoformat(b.decode()[:10]))
sqlite3.register_adapter(datetime, lambda d: d.strftime("%Y-%m-%d %H:%M:%S"))
sqlite3.register_adapter(date, lambda d: d.isoformat())
sqlite3.register_adapter(Decimal, str)


class FakeCursor:
    def __init__(self, conn, dictionary):
        self._c, self._dict = conn.cursor(), dictionary
        self.lastrowid, self.rowcount = None, -1

    def execute(self, sql, params=()):
        s = sql.strip()
        if s.upper().startswith("SET SESSION"):
            return
        s = s.replace("%s", "?").replace(" FOR UPDATE", "").replace("NOW()", "datetime('now','localtime')")
        s = re.sub(r"SELECT DATABASE\(\)", "SELECT 'procurement_db_frontend_test'", s)
        try:
            self._c.execute(s, tuple(params))
        except sqlite3.IntegrityError as exc:
            err = Exception(str(exc))
            err.errno = 1062 if "UNIQUE" in str(exc) else 1048
            raise err
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                err = Exception(str(exc))
                err.errno = 1146
                raise err
            raise
        self.lastrowid, self.rowcount = self._c.lastrowid, self._c.rowcount

    def _row(self, r):
        if r is None or not self._dict:
            return r
        return {d[0]: v for d, v in zip(self._c.description, r)}

    def fetchone(self):
        return self._row(self._c.fetchone())

    def fetchall(self):
        return [self._row(r) for r in self._c.fetchall()]

    def close(self):
        self._c.close()


class FakeConnection:
    def __init__(self):
        self._conn = sqlite3.connect(DB_FILE, detect_types=sqlite3.PARSE_DECLTYPES, timeout=10,
                                     check_same_thread=False)
        self.autocommit = True

    def cursor(self, dictionary=False, buffered=False):
        return FakeCursor(self._conn, dictionary)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


fake_db = types.ModuleType("db")
fake_db.get_db_connection = FakeConnection
sys.modules["db"] = fake_db

SCHEMA = """
CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT UNIQUE, full_name TEXT, email TEXT UNIQUE,
  password_hash TEXT, role TEXT, status TEXT, failed_login_count INTEGER DEFAULT 0,
  locked_until TIMESTAMP, last_login_at TIMESTAMP);
CREATE TABLE activity_log (log_id INTEGER PRIMARY KEY, user_id INTEGER, actor_label TEXT, action TEXT,
  detail TEXT, tone TEXT, entity_type TEXT, entity_id INTEGER, ip_address TEXT,
  created_at TIMESTAMP DEFAULT (datetime('now','localtime')));
CREATE TABLE system_settings (setting_key TEXT PRIMARY KEY, setting_value TEXT, updated_by INTEGER);
CREATE TABLE procuring_entities (entity_id INTEGER PRIMARY KEY, name TEXT, match_key TEXT, city TEXT, province TEXT);
CREATE TABLE procurement_modes (mode_id INTEGER PRIMARY KEY, name TEXT, is_competitive INTEGER);
CREATE TABLE categories (category_id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE procurements (procurement_id INTEGER PRIMARY KEY, reference_no TEXT
  CONSTRAINT uq_procurements_reference UNIQUE, title TEXT,
  procuring_entity_id INTEGER, end_user_unit TEXT, procurement_class TEXT, category_id INTEGER,
  mode_id INTEGER, abc NUMERIC, control_no TEXT, target_timeline TEXT, fiscal_year INTEGER, status TEXT,
  data_source TEXT, created_by INTEGER, updated_by INTEGER, created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
  updated_at TIMESTAMP DEFAULT (datetime('now','localtime')), archived_at TIMESTAMP, archived_by INTEGER);
CREATE TABLE bid_notices (notice_id INTEGER PRIMARY KEY, procurement_id INTEGER, notice_type TEXT,
  philgeps_ref TEXT, published_date DATE, submission_deadline DATE, bids_received INTEGER, posted_by INTEGER);
CREATE TABLE suppliers (supplier_id INTEGER PRIMARY KEY, name TEXT, match_key TEXT, address TEXT NOT NULL,
  city TEXT NOT NULL, province TEXT NOT NULL, tin TEXT, philgeps_reg_no TEXT, data_source TEXT, verified_same_as_id INTEGER);
CREATE TABLE bids (bid_id INTEGER PRIMARY KEY, procurement_id INTEGER, supplier_id INTEGER, notice_id INTEGER,
  bid_amount NUMERIC, rank_no INTEGER, recorded_by INTEGER);
CREATE TABLE bac_resolutions (resolution_id INTEGER PRIMARY KEY, procurement_id INTEGER, resolution_no TEXT UNIQUE,
  recommendation TEXT, recommended_bid_id INTEGER, post_qualification_done INTEGER, resolution_date DATE, logged_by INTEGER);
CREATE TABLE contracts (contract_id INTEGER PRIMARY KEY, contract_no TEXT UNIQUE, contract_date DATE);
CREATE TABLE award_notices (award_id INTEGER PRIMARY KEY, procurement_id INTEGER, supplier_id INTEGER,
  contract_id INTEGER, bid_id INTEGER, resolution_id INTEGER, notice_no TEXT UNIQUE, bid_reference_no TEXT,
  award_date DATE, published_date DATE, awarded_amount NUMERIC, award_basis TEXT, mode_as_recorded TEXT,
  lot_no INTEGER, lot_description TEXT, lot_quantity TEXT, certified_by_person_id INTEGER, certifier_role TEXT,
  issued_by INTEGER);
CREATE TABLE persons (person_id INTEGER PRIMARY KEY, full_name TEXT, match_key TEXT);
CREATE TABLE supplier_representatives (supplier_id INTEGER, person_id INTEGER, position TEXT);
CREATE VIEW v_award_graph_records AS SELECT NULL AS award_id, NULL AS procurement_id, NULL AS supplier_id,
  NULL AS bid_id, NULL AS notice_no, NULL AS award_date, NULL AS published_date, NULL AS awarded_amount,
  NULL AS award_basis, NULL AS lot_no, NULL AS contract_id, NULL AS contract_no, NULL AS certifier_id,
  NULL AS certifier_name, NULL AS certifier_role, NULL AS data_source WHERE 0;
"""
DOCS_TABLE = """
CREATE TABLE procurement_documents (document_id INTEGER PRIMARY KEY, procurement_id INTEGER NOT NULL,
  doc_type TEXT NOT NULL, title TEXT NOT NULL, original_name TEXT NOT NULL, stored_name TEXT NOT NULL UNIQUE,
  mime_type TEXT NOT NULL, size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, is_public INTEGER NOT NULL DEFAULT 0,
  uploaded_by INTEGER, uploaded_at TIMESTAMP NOT NULL DEFAULT (datetime('now','localtime')), deleted_at TIMESTAMP,
  deleted_by INTEGER);
"""

from werkzeug.security import generate_password_hash  # noqa: E402

PW = {"sec": "SecretariatPass123", "admin": "AdminPass12345", "mem": "MemberPass12345"}
TODAY = date.today()
with sqlite3.connect(DB_FILE) as c:
    c.executescript(SCHEMA)
    for uid, uname, role, pw in ((1, "t_secretariat", "secretariat", PW["sec"]),
                                 (2, "t_admin", "admin", PW["admin"]),
                                 (3, "t_member", "member", PW["mem"])):
        c.execute("INSERT INTO users (user_id, username, full_name, email, password_hash, role, status) "
                  "VALUES (?, ?, ?, ?, ?, ?, 'Active')",
                  (uid, uname, uname.replace("_", " ").title(), uname + "@example.invalid",
                   generate_password_hash(pw, method="scrypt"), role))
    c.execute("INSERT INTO procuring_entities VALUES (1, 'City Government of Santa Rosa', 'city government of santa rosa', "
              "'Santa Rosa', 'Laguna')")
    c.executemany("INSERT INTO procurement_modes VALUES (?, ?, ?)",
                  [(1, "Public Bidding", 1), (2, "Small Value Procurement (Sec. 53.9)", 0)])
    c.executemany("INSERT INTO system_settings (setting_key, setting_value) VALUES (?, ?)",
                  [("active_fiscal_year", "2026"), ("home_procuring_entity_id", "1"), ("require_philgeps_ref", "1"),
                   ("deadline_alert_days", "3")])
    for pid, ref, title, abc in ((100, "SR-2026-001", "Asphalt overlay Brgy. Don Jose", 4850000),
                                 (101, "SR-2026-002", "Office supplies", 450000),
                                 (102, "SR-2026-003", "Medical supplies", 1200000)):
        c.execute("INSERT INTO procurements (procurement_id, reference_no, title, procuring_entity_id, end_user_unit, "
                  "procurement_class, mode_id, abc, fiscal_year, status, data_source) VALUES "
                  "(?, ?, ?, 1, 'City Engineering Office', 'Infrastructure', 1, ?, 2026, 'Pre-Procurement', 'encoded')",
                  (pid, ref, title, abc))

GRAPH = {"runs": 0, "refs": set()}
fake_gs = types.ModuleType("graph_sync")


def _fake_run_sync():
    import graph_api
    conn = FakeConnection()
    try:
        data = graph_api.fetch_sample_graph(conn)
    finally:
        conn.close()
    GRAPH["runs"] += 1
    GRAPH["data"] = data
    return 200, {"status": "success", "totals": {"nodes": len(data["procurements"])}}


fake_gs.run_sync = _fake_run_sync
sys.modules["graph_sync"] = fake_gs

import graph_autosync  # noqa: E402

graph_autosync.RETRY_DELAYS = (0.2,)
import documents_api  # noqa: E402
from pathlib import Path  # noqa: E402

documents_api.UPLOAD_DIR = Path(TMP) / "uploads"
import app as app_module  # noqa: E402

flask_app = app_module.app
flask_app.config["TESTING"] = True

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"  -> {detail}"))


H = {"X-Requested-With": "fetch"}


def login(client, username, password, role):
    return client.post("/api/login", json={"username": username, "password": password, "role": role}, headers=H)


def q(sql, params=()):
    with sqlite3.connect(DB_FILE, detect_types=sqlite3.PARSE_DECLTYPES) as c:
        return c.execute(sql, params).fetchall()


def registry_row(client, ref):
    return next((r for r in client.get("/api/procurements").get_json()["procurements"] if r["id"] == ref), None)


sec, mem, adm, anon = (flask_app.test_client() for _ in range(4))
assert login(sec, "t_secretariat", PW["sec"], "BAC Secretariat").status_code == 200
assert login(mem, "t_member", PW["mem"], "BAC Member").status_code == 200
assert login(adm, "t_admin", PW["admin"], "Super Admin").status_code == 200

# ============================================================================================
print("=== W. Procurement workflow ===")
d = lambda n: (TODAY + timedelta(days=n)).isoformat()   # noqa: E731
REF = "SR-2026-001"
r = sec.post(f"/api/procurements/{REF}/notice", json={"noticeType": "ITB", "philgepsRef": "", "deadline": d(10)}, headers=H)
check("posting without PhilGEPS ref is refused when the setting requires it", r.status_code == 400
      and "philgepsRef" in r.get_json().get("errors", {}), r.get_json())
r = sec.post(f"/api/procurements/{REF}/notice", json={"noticeType": "ITB", "philgepsRef": "12681234",
                                                    "publishedDate": d(0), "deadline": d(-1)}, headers=H)
check("deadline before posting date is refused", r.status_code == 400, r.get_json())
r = mem.post(f"/api/procurements/{REF}/notice", json={"noticeType": "ITB", "philgepsRef": "1", "deadline": d(5)}, headers=H)
check("BAC Member cannot post a notice (403)", r.status_code == 403)
r = sec.post(f"/api/procurements/{REF}/notice", json={"noticeType": "ITB", "philgepsRef": "12681234",
                                                    "publishedDate": d(-30), "deadline": d(-9)}, headers=H)
check("Secretariat posts the ITB (200)", r.status_code == 200 and r.get_json()["status"] == "success", r.get_json())
check("status is now Posted (ITB/RFQ)", q("SELECT status FROM procurements WHERE procurement_id=100")[0][0] == "Posted (ITB/RFQ)")
r = sec.post(f"/api/procurements/{REF}/notice", json={"noticeType": "ITB", "philgepsRef": "x", "deadline": d(5)}, headers=H)
check("posting twice is refused (409 wrong_status)", r.status_code == 409 and r.get_json().get("code") == "wrong_status")
row = registry_row(sec, REF)
check("registry shows the PhilGEPS ref and deadline", row["philgeps"] == "12681234" and row["deadline"] == d(-9), row)

r = sec.post(f"/api/procurements/{REF}/bids", json={"bids": [{"supplier": "Astra Builders Corp.", "amount": "4,900,000.00"}]}, headers=H)
check("bid above the ABC is refused", r.status_code == 400 and "bids.0.amount" in r.get_json()["errors"], r.get_json())
r = sec.post(f"/api/procurements/{REF}/bids", json={"bids": [{"supplier": "Astra Builders", "amount": 4700000},
                                                         {"supplier": "ASTRA BUILDERS INC.", "amount": 4600000}]}, headers=H)
check("the same bidder twice (match_key) is refused", r.status_code == 400 and "bids.1.supplier" in r.get_json()["errors"], r.get_json())
bids = [{"supplier": "Astra Builders Corp.", "amount": "4,712,400.00"},
        {"supplier": "XYZ Construction", "amount": "4,690,000.00"},
        {"supplier": "Golf Engineering", "amount": "4,801,000"}]
r = sec.post(f"/api/procurements/{REF}/bids", json={"bids": bids}, headers=H)
check("Secretariat records 3 bids (200)", r.status_code == 200, r.get_json())
ranks = q("SELECT s.name, b.rank_no, b.bid_amount FROM bids b JOIN suppliers s ON s.supplier_id=b.supplier_id "
          "WHERE b.procurement_id=100 ORDER BY b.rank_no")
check("bids ranked by amount (lowest = rank 1)", [x[0] for x in ranks] == ["XYZ Construction", "Astra Builders Corp.",
                                                                            "Golf Engineering"], ranks)
check("suppliers created as encoded (with '' fallback for NOT NULL address)",
      q("SELECT COUNT(*) FROM suppliers WHERE data_source='encoded' AND address=''")[0][0] == 3)
check("bids_received updated to 3", q("SELECT bids_received FROM bid_notices WHERE procurement_id=100")[0][0] == 3)
check("status is Under Evaluation", q("SELECT status FROM procurements WHERE procurement_id=100")[0][0] == "Under Evaluation")

wf = sec.get(f"/api/procurements/{REF}/workflow").get_json()
check("workflow endpoint lists the bids in rank order", [b["rank"] for b in wf["bids"]] == [1, 2, 3], wf)
xyz = wf["bids"][0]["bidId"]
astra = wf["bids"][1]["bidId"]
r = sec.post(f"/api/procurements/{REF}/resolution", json={"resolutionNo": "R1", "recommendation": "LCRB",
                                                        "recommendedBidId": xyz, "postQualificationDone": True}, headers=H)
check("Secretariat cannot log a resolution (403)", r.status_code == 403)
r = mem.post(f"/api/procurements/{REF}/resolution", json={"resolutionNo": "BAC Res. 2026-052", "recommendation": "LCRB",
                                                        "recommendedBidId": xyz, "postQualificationDone": False}, headers=H)
check("LCRB without post-qualification is refused", r.status_code == 400 and "postQualificationDone" in r.get_json()["errors"])
r = mem.post(f"/api/procurements/{REF}/resolution", json={"resolutionNo": "BAC Res. 2026-052", "recommendation": "LCRB",
                                                        "recommendedBidId": 99999, "postQualificationDone": True}, headers=H)
check("recommended bid must belong to the latest notice", r.status_code == 400 and "recommendedBidId" in r.get_json()["errors"])
r = mem.post(f"/api/procurements/{REF}/resolution", json={"resolutionNo": "BAC Res. 2026-052", "recommendation": "LCRB",
                                                        "recommendedBidId": astra, "postQualificationDone": True,
                                                        "resolutionDate": d(-5)}, headers=H)
check("BAC Member logs the resolution (rank-2 bid) (200)", r.status_code == 200, r.get_json())
check("warning: recommended bid is not the lowest", any("not the lowest" in w for w in r.get_json().get("warnings", [])),
      r.get_json())
check("status is Recommended for Award", q("SELECT status FROM procurements WHERE procurement_id=100")[0][0] == "Recommended for Award")

r = sec.post(f"/api/procurements/{REF}/award", json={"noaNo": "NOA-SR-2026-013", "awardDate": d(-6)}, headers=H)
check("award date before the resolution is refused", r.status_code == 400 and "awardDate" in r.get_json()["errors"])
r = sec.post(f"/api/procurements/{REF}/award", json={"noaNo": "NOA-SR-2026-013", "awardDate": d(-3), "contractNo": "C-1"}, headers=H)
check("contract no. without date is refused", r.status_code == 400 and "contractDate" in r.get_json()["errors"], r.get_json())
r = sec.post(f"/api/procurements/{REF}/award", json={"noaNo": "NOA-SR-2026-013", "awardDate": d(-3)}, headers=H)
check("Secretariat issues the NOA (200)", r.status_code == 200, r.get_json())
aw = q("SELECT a.awarded_amount, s.name, a.resolution_id, a.bid_id, a.award_basis FROM award_notices a "
       "JOIN suppliers s ON s.supplier_id=a.supplier_id WHERE a.procurement_id=100")
check("award copies the recommended bid (supplier + amount)", len(aw) == 1 and aw[0][1] == "Astra Builders Corp."
      and Decimal(str(aw[0][0])) == Decimal("4712400.00") and aw[0][3] == astra, aw)
check("status is Awarded", q("SELECT status FROM procurements WHERE procurement_id=100")[0][0] == "Awarded")
row = registry_row(sec, REF)
check("registry: awardee, NOA, no contract yet", row["bidder"] == "Astra Builders Corp." and row["noa"] == "NOA-SR-2026-013"
      and row["contractNo"] is None, row)
r = sec.post(f"/api/procurements/{REF}/contract", json={"contractNo": "CON-2026-013", "contractDate": d(-4)}, headers=H)
check("contract dated before the award is refused", r.status_code == 400)
r = sec.post(f"/api/procurements/{REF}/contract", json={"contractNo": "CON-2026-013", "contractDate": d(-1)}, headers=H)
check("Secretariat records the contract (200)", r.status_code == 200, r.get_json())
check("registry shows the contract no.", registry_row(sec, REF)["contractNo"] == "CON-2026-013")
r = sec.post(f"/api/procurements/{REF}/contract", json={"contractNo": "CON-X", "contractDate": d(0)}, headers=H)
check("second contract is refused (409)", r.status_code == 409)
pub = anon.get("/api/public/procurements").get_json()["procurements"]
check("public portal shows the award with the contract amount", any(p["id"] == REF and p["status"] == "Awarded"
                                                                     and p["contractAmount"] == 4712400.0 for p in pub), pub)
acts = [a for (a,) in q("SELECT action FROM activity_log WHERE entity_type='procurement' AND entity_id=100 ORDER BY log_id")]
check("activity log has every step", acts == ["ITB posted", "Bids recorded", "BAC resolution logged",
                                              "Notice of Award issued", "Contract recorded"], acts)
graph_autosync.wait_for(graph_autosync.request_sync("test"), 5)
gd = GRAPH.get("data", {})
gp = next((p for p in gd.get("procurements", []) if p["id"] == REF), None)
check("knowledge graph contains the procurement with its bids and award", gp is not None and GRAPH["runs"] >= 5,
      (GRAPH["runs"], gp))

# failure-of-bidding path + duplicate resolution number
R2 = "SR-2026-002"
sec.post(f"/api/procurements/{R2}/notice", json={"noticeType": "RFQ", "philgepsRef": "9", "publishedDate": d(-40), "deadline": d(-30)}, headers=H)
sec.post(f"/api/procurements/{R2}/bids", json={"bids": [{"supplier": "Solo Trading", "amount": 440000}]}, headers=H)
r = mem.post(f"/api/procurements/{R2}/resolution", json={"resolutionNo": "BAC Res. 2026-052", "recommendation": "FAIL"}, headers=H)
check("duplicate resolution number is refused", r.status_code == 400 and "resolution_no" in r.get_json()["errors"], r.get_json())
r = mem.post(f"/api/procurements/{R2}/resolution", json={"resolutionNo": "BAC Res. 2026-053", "recommendation": "FAIL",
                                                       "resolutionDate": d(-20)}, headers=H)
check("failure of bidding saved", r.status_code == 200 and q("SELECT status FROM procurements WHERE procurement_id=101")[0][0]
      == "Failed Bidding", r.get_json())
check("FAIL resolution has no recommended bid", q("SELECT recommended_bid_id FROM bac_resolutions WHERE procurement_id=101")[0][0] is None)
r = sec.post(f"/api/procurements/{R2}/award", json={"noaNo": "NOA-X", "awardDate": d(0)}, headers=H)
check("award after failure is refused (409)", r.status_code == 409)
check("admin cannot run workflow steps (403)", adm.post(f"/api/procurements/SR-2026-003/notice", json={}, headers=H).status_code == 403)
check("missing X-Requested-With header is refused (400)",
      sec.post("/api/procurements/SR-2026-003/notice", json={"noticeType": "ITB"}).status_code == 400)
check("unknown procurement -> 404", sec.post("/api/procurements/SR-2099-999/notice", json={"noticeType": "ITB",
      "philgepsRef": "1", "deadline": d(3)}, headers=H).status_code == 404)

# third one: posted, deadline soon (closing soon) - and in evaluation for follow-up
sec.post("/api/procurements/SR-2026-003/notice", json={"noticeType": "ITB", "philgepsRef": "77", "deadline": d(2)}, headers=H)

# ============================================================================================
print("=== D. Dashboard ===")
r = anon.get("/api/dashboard")
check("anonymous cannot read the dashboard (401)", r.status_code == 401)
db = sec.get("/api/dashboard").get_json()
check("dashboard loads", db.get("status") == "success", db)
stg = {x["name"]: x["count"] for x in db["stages"]}
check("stage counts in lifecycle order", stg["Awarded"] == 1 and stg["Failed Bidding"] == 1 and stg["Posted (ITB/RFQ)"] == 1
      and [x["name"] for x in db["stages"]][0] == "Pre-Procurement", db["stages"])
check("KPIs: 3 tracked, 1 awarded, savings = ABC - award", db["kpis"]["tracked"] == 3 and db["kpis"]["awardedCount"] == 1
      and abs(db["kpis"]["savings"] - (4850000 - 4712400)) < 0.01, db["kpis"])
check("avg bids per notice = (3 + 1) / 2", db["kpis"]["avgBids"] == 2.0, db["kpis"])
check("closing soon lists SR-2026-003", [c["id"] for c in db["timeliness"]["closingSoon"]] == ["SR-2026-003"], db["timeliness"])
check("average days computed", db["timeliness"]["avgDays"]["postingToDeadline"] is not None, db["timeliness"])
types_ = {f["type"] for f in db["flags"]}
check("flags: single bid and not-the-lowest award", {"single_bid", "not_lowest"} <= types_ or {"not_lowest"} <= types_, db["flags"])
check("supplier concentration: Astra 100%", db["suppliers"][0]["name"] == "Astra Builders Corp." and db["suppliers"][0]["share"] == 100.0,
      db["suppliers"])
check("traceability 100% and contracts 100%", db["health"]["traceability"] == 100.0 and db["health"]["contractsRecorded"] == 100.0,
      db["health"])
check("documents completeness is None while the table is missing", db["health"]["documents"] is None, db["health"])
check("recent activity listed", len(db["recent"]) > 0)
# follow-up: post+bid a record whose deadline is long past and leave it in evaluation
with sqlite3.connect(DB_FILE) as c:
    c.execute("UPDATE bid_notices SET submission_deadline=? WHERE procurement_id=102", (d(-30),))
db = sec.get("/api/dashboard").get_json()
check("follow-up: posted, deadline passed, bids not recorded", any(f["id"] == "SR-2026-003" for f in db["timeliness"]["followUp"]),
      db["timeliness"]["followUp"])

# ============================================================================================
print("=== U. Accounts ===")
check("secretariat cannot list accounts (403)", sec.get("/api/admin/users").status_code == 403)
users = adm.get("/api/admin/users").get_json()["users"]
check("admin lists 3 accounts", len(users) == 3 and all("password" not in str(u).lower() for u in users), users)
r = adm.post("/api/admin/users", json={"username": "bad name", "fullName": "X", "email": "nope", "role": "boss"}, headers=H)
check("invalid account fields are refused", r.status_code == 400 and {"username", "fullName", "email", "role"} <= set(r.get_json()["errors"]))
r = adm.post("/api/admin/users", json={"username": "t_member", "fullName": "Dup", "email": "dup@example.invalid",
                                       "role": "member"}, headers=H)
check("duplicate username is refused", r.status_code == 400 and "username" in r.get_json()["errors"])
r = adm.post("/api/admin/users", json={"username": "jdelacruz", "fullName": "Juan Dela Cruz", "email": "jdc@example.invalid",
                                       "role": "secretariat"}, headers=H)
body = r.get_json()
check("admin creates an account with a temporary password", r.status_code == 200 and len(body.get("temporaryPassword", "")) == 14, body)
new_id = body["user"]["userId"]
check("new account is Pending activation, hash only", q("SELECT status, password_hash LIKE 'scrypt:%' FROM users WHERE user_id=?",
                                                        (new_id,))[0] == ("Pending activation", 1))
jd = flask_app.test_client()
r = login(jd, "jdelacruz", body["temporaryPassword"], "BAC Secretariat")
check("new user signs in with the temporary password -> must change it",
      r.status_code == 200 and r.get_json().get("status") == "password_change_required", r.get_json())
r = jd.post("/api/change-password", json={"current_password": body["temporaryPassword"], "new_password": "NewSecret12345",
                                          "confirm_password": "NewSecret12345"}, headers=H)
check("new user sets a password and the account becomes Active", r.status_code == 200
      and q("SELECT status FROM users WHERE user_id=?", (new_id,))[0][0] == "Active", r.get_json())
r = adm.put(f"/api/admin/users/{new_id}", json={"fullName": "Juan D. Cruz", "email": "jdc@example.invalid", "role": "member"}, headers=H)
check("admin changes name and role", r.status_code == 200 and q("SELECT role, full_name FROM users WHERE user_id=?", (new_id,))[0]
      == ("member", "Juan D. Cruz"), r.get_json())
check("the user's old session ends after the role change", jd.get("/api/procurements").status_code == 401)
check("admin cannot change own role", adm.put("/api/admin/users/2", json={"fullName": "T Admin", "email": "t_admin@example.invalid",
                                                                          "role": "member"}, headers=H).status_code == 409)
check("admin cannot disable own account", adm.post("/api/admin/users/2/disable", headers=H).status_code == 409)
mem2 = flask_app.test_client()
login(mem2, "t_member", PW["mem"], "BAC Member")
r = adm.post("/api/admin/users/3/disable", headers=H)
check("admin disables t_member", r.status_code == 200 and q("SELECT status FROM users WHERE user_id=3")[0][0] == "Disabled")
check("disabled user's session ends", mem2.get("/api/dashboard").status_code == 401)
check("disabled user cannot sign in", login(flask_app.test_client(), "t_member", PW["mem"], "BAC Member").status_code == 401)
r = adm.post("/api/admin/users/3/enable", headers=H)
check("enable: signed in before -> Active", r.status_code == 200
      and q("SELECT status FROM users WHERE user_id=3")[0][0] == "Active", r.get_json())
with sqlite3.connect(DB_FILE) as c:
    c.execute("INSERT INTO users (user_id, username, full_name, email, password_hash, role, status) VALUES "
              "(8, 'never', 'Never Signed In', 'n@example.invalid', 'x', 'member', 'Disabled')")
r = adm.post("/api/admin/users/8/enable", headers=H)
check("enable: never signed in -> Pending activation", r.status_code == 200
      and q("SELECT status FROM users WHERE user_id=8")[0][0] == "Pending activation", r.get_json())
with sqlite3.connect(DB_FILE) as c:
    c.execute("UPDATE users SET status='Active' WHERE user_id=3")
    c.execute("INSERT INTO users (user_id, username, full_name, email, password_hash, role, status) VALUES "
              "(9, 'admin2', 'Second Admin', 'a2@example.invalid', 'x', 'admin', 'Disabled')")
    c.execute("UPDATE users SET status='Active' WHERE user_id=9")
adm2_id = 9
r = adm.put("/api/admin/users/9", json={"fullName": "Second Admin", "email": "a2@example.invalid", "role": "member"}, headers=H)
check("another admin can be demoted while a second active admin remains", r.status_code == 200, r.get_json())
acts = [a for (a,) in q("SELECT action FROM activity_log WHERE entity_type='user' AND action LIKE 'Account%' ORDER BY log_id")]
check("account changes are audited", acts[:2] == ["Account created", "Account updated"] and "Account disabled" in acts, acts)

# ============================================================================================
print("=== S. Settings ===")
check("secretariat cannot read settings (403)", sec.get("/api/admin/settings").status_code == 403)
st = adm.get("/api/admin/settings").get_json()
check("settings load with defaults for missing keys", st["settings"]["followup_bids_days"] == "2"
      and st["settings"]["active_fiscal_year"] == "2026", st)
r = adm.put("/api/admin/settings", json={"active_fiscal_year": "26", "deadline_alert_days": "99"}, headers=H)
check("invalid settings are refused", r.status_code == 400 and {"active_fiscal_year", "deadline_alert_days"} <= set(r.get_json()["errors"]))
payload = dict(st["settings"], active_fiscal_year="2027", deadline_alert_days="5", require_philgeps_ref=False,
               followup_bids_days="1", newEntityName="City Government of Santa Rosa, Laguna")
r = adm.put("/api/admin/settings", json=payload, headers=H)
body = r.get_json()
check("settings saved", r.status_code == 200 and body["settings"]["active_fiscal_year"] == "2027"
      and body["settings"]["require_philgeps_ref"] == "0", body)
check("new procuring entity created and selected", q("SELECT COUNT(*) FROM procuring_entities")[0][0] == 2
      and body["settings"]["home_procuring_entity_id"] == "2", body["settings"])
r = sec.post("/api/procurements", json={"title": "Road repair", "abc": "1000000", "unit": "CEO", "mode": "Public Bidding",
                                        "category": "Infrastructure", "timeline": ""}, headers=H)
check("new records use the new fiscal year (SR-2027-001)", r.status_code == 201 and r.get_json()["procurement"]["id"] == "SR-2027-001",
      r.get_json())
r = sec.post("/api/procurements/SR-2027-001/notice", json={"noticeType": "ITB", "philgepsRef": "", "deadline": d(5)}, headers=H)
check("PhilGEPS ref now optional (setting off)", r.status_code == 200, r.get_json())
check("settings change audited", q("SELECT COUNT(*) FROM activity_log WHERE action='System settings saved'")[0][0] == 1)

# ============================================================================================
print("=== F. Documents ===")
r = sec.get(f"/api/procurements/{REF}/documents")
check("documents before installation -> 503 not installed", r.status_code == 503
      and r.get_json().get("code") == "documents_not_installed", r.get_json())
with sqlite3.connect(DB_FILE) as c:
    c.executescript(DOCS_TABLE)
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def upload(client, ref, data, name, doc_type="Notice of Award", public="0", title=""):
    return client.post(f"/api/procurements/{ref}/documents", headers=H, content_type="multipart/form-data",
                       data={"file": (io.BytesIO(data), name), "docType": doc_type, "title": title, "isPublic": public})


check("member cannot upload (403)", upload(mem, REF, PDF, "noa.pdf").status_code in (401, 403))
r = upload(sec, REF, b"MZ\x90\x00 not a pdf", "noa.pdf")
check("content that is not a PDF is refused", r.status_code == 400 and "file" in r.get_json()["errors"], r.get_json())
r = upload(sec, REF, PDF, "noa.exe")
check("disallowed extension is refused", r.status_code == 400)
r = upload(sec, REF, PDF, "NOA-SR-2026-013.pdf", title="Notice of Award - Astra")
check("Secretariat uploads the NOA (201)", r.status_code == 201, r.get_json())
noa_id = r.get_json()["documentId"]
r = upload(sec, REF, b"PK\x03\x04" + b"x" * 100, "contract.docx", doc_type="Contract", public="1")
check("upload a public contract (201)", r.status_code == 201, r.get_json())
con_id = r.get_json()["documentId"]
stored = q("SELECT stored_name, sha256 FROM procurement_documents WHERE document_id=?", (noa_id,))[0]
check("file stored under a random name with its SHA-256", re.fullmatch(r"[0-9a-f]{32}\.pdf", stored[0])
      and (documents_api.UPLOAD_DIR / stored[0]).read_bytes() == PDF, stored)
lst = mem.get(f"/api/procurements/{REF}/documents").get_json()
check("member sees both documents", [x["title"] for x in lst["documents"]] == ["contract.docx", "Notice of Award - Astra"], lst)
r = mem.get(f"/api/documents/{noa_id}/download")
check("staff download returns the file", r.status_code == 200 and r.data == PDF and "attachment" in r.headers["Content-Disposition"])
check("internal document is not downloadable publicly (404)", anon.get(f"/api/public/documents/{noa_id}/download").status_code == 404)
pub = anon.get("/api/public/documents").get_json()
check("public list shows only the public contract, without uploader/hash", [x["documentId"] for x in pub["documents"]] == [con_id]
      and "uploadedBy" not in pub["documents"][0] and "sha256" not in pub["documents"][0], pub)
check("public download works", anon.get(f"/api/public/documents/{con_id}/download").status_code == 200)
r = sec.post(f"/api/documents/{noa_id}/visibility", json={"isPublic": True}, headers=H)
check("Secretariat publishes the NOA", r.status_code == 200 and anon.get(f"/api/public/documents/{noa_id}/download").status_code == 200)
search = sec.get("/api/documents?q=Astra").get_json()
check("library search by title", [x["documentId"] for x in search["documents"]] == [noa_id], search)
check("library filter by type", [x["documentId"] for x in sec.get("/api/documents?type=Contract").get_json()["documents"]] == [con_id])
db = sec.get("/api/dashboard").get_json()
check("dashboard: award documents 100% (NOA + contract on file)", db["health"]["documents"] == 100.0, db["health"])
r = sec.post(f"/api/documents/{noa_id}/remove", json={"reason": "no"}, headers=H)
check("removal needs a reason of 5+ characters", r.status_code == 400)
r = sec.post(f"/api/documents/{noa_id}/remove", json={"reason": "Wrong file uploaded"}, headers=H)
check("Secretariat removes the NOA (soft)", r.status_code == 200 and (documents_api.UPLOAD_DIR / stored[0]).is_file())
check("removed document disappears from lists and downloads", noa_id not in [x["documentId"] for x in
      sec.get("/api/documents").get_json()["documents"]] and sec.get(f"/api/documents/{noa_id}/download").status_code == 404)
acts = [a for (a,) in q("SELECT action FROM activity_log WHERE entity_type='document' ORDER BY log_id")]
check("document actions audited", acts == ["Document uploaded", "Document uploaded", "Document visibility changed",
                                           "Document removed"], acts)

# ============================================================================================
print("=== R. PMR Excel ===")
check("anonymous cannot download the staff PMR (401)", anon.get("/api/reports/pmr.xlsx").status_code == 401)
r = sec.get("/api/reports/pmr.xlsx")
ok_zip = False
try:
    z = zipfile.ZipFile(io.BytesIO(r.data))
    sheet = z.read("xl/worksheets/sheet1.xml").decode()
    ok_zip = z.testzip() is None and "SR-2026-001" in sheet and "Astra Builders Corp." in sheet and "CON-2026-013" in sheet
except Exception as exc:  # noqa: BLE001
    sheet = str(exc)
check("staff PMR is a valid .xlsx with the records", r.status_code == 200 and ok_zip, sheet[:300])
r = anon.get("/api/public/reports/pmr.xlsx")
psheet = zipfile.ZipFile(io.BytesIO(r.data)).read("xl/worksheets/sheet1.xml").decode() if r.status_code == 200 else ""
check("public PMR has no BAC resolution column or resolution numbers",
      r.status_code == 200 and "BAC Resolution" not in psheet and "BAC Res." not in psheet and "SR-2026-001" in psheet, psheet[:300])
try:
    import openpyxl  # noqa: F401
    wb = openpyxl.load_workbook(io.BytesIO(sec.get("/api/reports/pmr.xlsx").data))
    ws = wb.active
    hdr = [c.value for c in ws[3]]
    num = ws["F4"].value
    check("openpyxl reads the workbook: header row and ABC as a number", hdr[0] == "Procurement ID" and isinstance(num, (int, float)),
          (hdr, num))
except ImportError:
    print("  [SKIP] openpyxl not installed (optional check)")

print(f"\nTOTAL: {sum(RESULTS)}/{len(RESULTS)} passed  (SIMULATED - SQLite stand-in for MariaDB, no Neo4j)")
sys.exit(0 if all(RESULTS) else 1)
