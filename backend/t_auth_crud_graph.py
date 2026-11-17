"""
backend/t_auth_crud_graph.py - ISOLATED, SIMULATED end-to-end tests (Flask test client).

Run from backend/:   python t_auth_crud_graph.py

Covers
  A. Forgot password: request from the sign-in page, Super Admin issues a temporary password,
     the user must set a new one, the old password stops working, dismiss, safety rules.
  B. Procurement CRUD as the BAC Secretariat: create, read, edit, archive, restore (and the
     list endpoints), including the rules that must refuse.
  C. Knowledge graph: every save triggers the automatic sync, and the record encoded in the
     Procurement Encoding tab is in the graph data (archived -> removed, restored -> back).

Safety: the real backend/db.py is never imported. MariaDB is replaced by a temporary SQLite file
(a stand-in: %s placeholders and FOR UPDATE are translated; MariaDB locking is NOT simulated).
graph_sync is replaced by a stand-in whose run_sync() reads the graph records with the REAL
graph_api.fetch_sample_graph() queries, so no Neo4j is needed. Everything here is SIMULATED.
"""
import os
import re
import sqlite3
import sys
import tempfile
import time
import types
from datetime import datetime
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
if "db" in sys.modules:
    sys.exit("TEST SAFETY: the real db module is already imported; run this file on its own.")

DB_FILE = os.path.join(tempfile.mkdtemp(prefix="bacpms-test-"), "test.sqlite")
sqlite3.register_converter("TIMESTAMP", lambda b: datetime.fromisoformat(b.decode()))
sqlite3.register_adapter(datetime, lambda d: d.strftime("%Y-%m-%d %H:%M:%S"))
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
        self._c.execute(s, tuple(params))
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
CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT UNIQUE, full_name TEXT, email TEXT,
  password_hash TEXT, role TEXT, status TEXT, failed_login_count INTEGER DEFAULT 0,
  locked_until TIMESTAMP, last_login_at TIMESTAMP);
CREATE TABLE activity_log (log_id INTEGER PRIMARY KEY, user_id INTEGER, actor_label TEXT, action TEXT,
  detail TEXT, tone TEXT, entity_type TEXT, entity_id INTEGER, ip_address TEXT,
  created_at TIMESTAMP DEFAULT (datetime('now','localtime')));
CREATE TABLE system_settings (setting_key TEXT PRIMARY KEY, setting_value TEXT, updated_by INTEGER);
CREATE TABLE procuring_entities (entity_id INTEGER PRIMARY KEY, name TEXT, city TEXT, province TEXT);
CREATE TABLE procurement_modes (mode_id INTEGER PRIMARY KEY, name TEXT, is_competitive INTEGER);
CREATE TABLE categories (category_id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE procurements (procurement_id INTEGER PRIMARY KEY, reference_no TEXT
  CONSTRAINT uq_procurements_reference UNIQUE, title TEXT,
  procuring_entity_id INTEGER, end_user_unit TEXT, procurement_class TEXT, category_id INTEGER,
  mode_id INTEGER, abc NUMERIC, control_no TEXT, target_timeline TEXT, fiscal_year INTEGER, status TEXT,
  data_source TEXT, created_by INTEGER, updated_by INTEGER, created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
  updated_at TIMESTAMP DEFAULT (datetime('now','localtime')), archived_at TIMESTAMP, archived_by INTEGER);
CREATE TABLE bid_notices (notice_id INTEGER PRIMARY KEY, procurement_id INTEGER, notice_type TEXT,
  philgeps_ref TEXT, published_date TIMESTAMP, submission_deadline TIMESTAMP, bids_received INTEGER);
CREATE TABLE suppliers (supplier_id INTEGER PRIMARY KEY, name TEXT, address TEXT, city TEXT, province TEXT,
  data_source TEXT);
CREATE TABLE bids (bid_id INTEGER PRIMARY KEY, procurement_id INTEGER, supplier_id INTEGER, notice_id INTEGER,
  bid_amount NUMERIC, rank_no INTEGER);
CREATE TABLE bac_resolutions (resolution_id INTEGER PRIMARY KEY, procurement_id INTEGER, resolution_no TEXT,
  recommendation TEXT, recommended_bid_id INTEGER, post_qualification_done INTEGER, resolution_date TIMESTAMP);
CREATE TABLE award_notices (award_id INTEGER PRIMARY KEY, procurement_id INTEGER, supplier_id INTEGER, bid_id INTEGER, contract_id INTEGER,
  notice_no TEXT, award_date TIMESTAMP, published_date TIMESTAMP, awarded_amount NUMERIC, award_basis TEXT);
CREATE TABLE persons (person_id INTEGER PRIMARY KEY, full_name TEXT);
CREATE TABLE supplier_representatives (supplier_id INTEGER, person_id INTEGER, position TEXT);
CREATE TABLE contracts (contract_id INTEGER PRIMARY KEY, contract_no TEXT, contract_date TIMESTAMP);
CREATE VIEW v_award_graph_records AS SELECT NULL AS award_id, NULL AS procurement_id, NULL AS supplier_id,
  NULL AS bid_id, NULL AS notice_no, NULL AS award_date, NULL AS published_date, NULL AS awarded_amount,
  NULL AS award_basis, NULL AS lot_no, NULL AS contract_id, NULL AS contract_no, NULL AS certifier_id,
  NULL AS certifier_name, NULL AS certifier_role, NULL AS data_source WHERE 0;
"""

from werkzeug.security import generate_password_hash  # noqa: E402

PW = {"sec": "SecretariatPass123", "admin": "AdminPass12345", "mem": "MemberPass12345"}
with sqlite3.connect(DB_FILE) as c:
    c.executescript(SCHEMA)
    for uid, uname, role, pw in ((1, "t_secretariat", "secretariat", PW["sec"]),
                                 (2, "t_admin", "admin", PW["admin"]),
                                 (3, "t_member", "member", PW["mem"])):
        c.execute("INSERT INTO users (user_id, username, full_name, email, password_hash, role, status) "
                  "VALUES (?, ?, ?, ?, ?, ?, 'Active')",
                  (uid, uname, uname.replace("_", " ").title(), uname + "@example.invalid",
                   generate_password_hash(pw, method="scrypt"), role))
    c.execute("INSERT INTO users (user_id, username, full_name, email, password_hash, role, status) "
              "VALUES (4, 't_disabled', 'Disabled User', 'd@example.invalid', 'x', 'member', 'Disabled')")
    c.execute("INSERT INTO procuring_entities VALUES (1, 'City Government of Santa Rosa', 'Santa Rosa', 'Laguna')")
    c.executemany("INSERT INTO procurement_modes VALUES (?, ?, ?)",
                  [(1, "Public Bidding", 1), (2, "Small Value Procurement", 0)])
    c.executemany("INSERT INTO system_settings (setting_key, setting_value) VALUES (?, ?)",
                  [("active_fiscal_year", "2026"), ("home_procuring_entity_id", "1")])
    c.execute("INSERT INTO procurements (procurement_id, reference_no, title, procuring_entity_id, end_user_unit, "
              "procurement_class, mode_id, abc, fiscal_year, status, data_source) VALUES "
              "(100, 'SAMPLE-2026-001', 'Sample laptops', 1, 'ICT Office', 'Goods', 1, 500000, 2026, "
              "'Pre-Procurement', 'sample')")

# ---- graph_sync stand-in: records what the graph would contain after each sync --------------
GRAPH = {"runs": 0, "refs": set(), "fail_next": 0}
fake_gs = types.ModuleType("graph_sync")


def _fake_run_sync():
    if GRAPH["fail_next"]:
        GRAPH["fail_next"] -= 1
        return 503, {"status": "error", "stage": "neo4j_connect", "message": "Neo4j is not reachable (simulated)."}
    import graph_api
    conn = FakeConnection()
    try:
        data = graph_api.fetch_sample_graph(conn)
    finally:
        conn.close()
    GRAPH["runs"] += 1
    GRAPH["refs"] = {p["id"] for p in data["procurements"]}
    GRAPH["sources"] = {p["id"]: p["dataSource"] for p in data["procurements"]}
    return 200, {"status": "success", "totals": {"nodes": len(data["procurements"])}}


fake_gs.run_sync = _fake_run_sync
sys.modules["graph_sync"] = fake_gs

import graph_autosync  # noqa: E402

graph_autosync.RETRY_DELAYS = (0.2,)          # fast retries in the test
import app as app_module  # noqa: E402  (starts the autosync worker + catch-up sync)

flask_app = app_module.app
flask_app.config["TESTING"] = True

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"  -> {detail}"))


H = {"X-Requested-With": "fetch"}


def login(client, username, password, role):
    return client.post("/api/login", json={"username": username, "password": password, "role": role}, headers=H)


def log_actions(user_id):
    with sqlite3.connect(DB_FILE) as c:
        return [r[0] for r in c.execute("SELECT action FROM activity_log WHERE entity_type='user' AND entity_id=? "
                                        "ORDER BY log_id", (user_id,))]


# ============================================================================================
print("=== A. Forgot password ===")
anon = flask_app.test_client()
r = anon.post("/api/forgot-password", json={"username": "nobody"}, headers=H)
r2 = anon.post("/api/forgot-password", json={"username": "t_member"}, headers=H)
check("unknown and known usernames get the same reply", r.status_code == r2.status_code == 200
      and r.get_json()["message"] == r2.get_json()["message"], (r.get_json(), r2.get_json()))
anon.post("/api/forgot-password", json={"username": "t_member"}, headers=H)
check("request is logged once (15-minute cooldown)", log_actions(3).count("Password reset requested") == 1,
      log_actions(3))
r = anon.post("/api/forgot-password", json={"username": "t_disabled"}, headers=H)
check("disabled account: same reply, nothing logged", r.status_code == 200 and not log_actions(4), log_actions(4))
r = anon.post("/api/forgot-password", json={"username": ""}, headers=H)
check("empty username -> 400", r.status_code == 400)
r = anon.post("/api/forgot-password", json={"username": "t_member"})
check("missing X-Requested-With header -> 400", r.status_code == 400)

r = anon.get("/api/admin/password-resets")
check("anonymous cannot list reset requests (401)", r.status_code == 401, r.status_code)
sec = flask_app.test_client()
check("secretariat signs in", login(sec, "t_secretariat", PW["sec"], "BAC Secretariat").status_code == 200)
check("secretariat cannot list reset requests (403)", sec.get("/api/admin/password-resets").status_code == 403)
check("secretariat cannot issue a temporary password (403)",
      sec.post("/api/admin/users/3/reset-password", headers=H).status_code == 403)

adm = flask_app.test_client()
check("super admin signs in", login(adm, "t_admin", PW["admin"], "Super Admin").status_code == 200)
body = adm.get("/api/admin/password-resets").get_json()
reqs = body.get("requests", [])
check("admin sees the open request for t_member", [q["username"] for q in reqs] == ["t_member"], body)
check("account list has 4 users, no password hashes", len(body.get("users", [])) == 4
      and all("password" not in str(u).lower() for u in body["users"]), body.get("users"))
check("admin cannot reset own account here", adm.post("/api/admin/users/2/reset-password", headers=H).status_code == 400)
check("disabled account cannot be reset", adm.post("/api/admin/users/4/reset-password", headers=H).status_code == 409)

member_old = flask_app.test_client()
check("member signed in before the reset", login(member_old, "t_member", PW["mem"], "BAC Member").status_code == 200)
r = adm.post("/api/admin/users/3/reset-password", headers=H)
temp = (r.get_json() or {}).get("temporaryPassword", "")
check("temporary password issued once (14 chars, letters+digits)", r.status_code == 200 and len(temp) == 14
      and re.search(r"[A-Za-z]", temp) and re.search(r"\d", temp), r.get_json())
check("response is not cached", r.headers.get("Cache-Control") == "no-store")
check("request is closed after the reset", adm.get("/api/admin/password-resets").get_json()["requests"] == [])
check("member's existing session ended", member_old.get("/api/procurements").status_code == 401)
check("old password no longer works",
      login(flask_app.test_client(), "t_member", PW["mem"], "BAC Member").status_code == 401)
mem = flask_app.test_client()
r = login(mem, "t_member", temp, "BAC Member")
check("temporary password -> password change required", r.status_code == 200
      and r.get_json()["status"] == "password_change_required", r.get_json())
check("restricted session cannot read records (403)", mem.get("/api/procurements").status_code == 403)
new_pw = "BrandNewPass2026"
r = mem.post("/api/change-password", json={"current_password": temp, "new_password": new_pw,
                                           "confirm_password": new_pw}, headers=H)
check("member sets a new password", r.status_code == 200 and r.get_json()["status"] == "success", r.get_json())
check("member can use the system again", mem.get("/api/procurements").status_code == 200)
check("temporary password no longer works",
      login(flask_app.test_client(), "t_member", temp, "BAC Member").status_code == 401)
RESET_FLOW = {"Password reset requested", "Temporary password issued", "Password changed - account activated"}
check("audit trail: requested -> issued -> changed", [a for a in log_actions(3) if a in RESET_FLOW] == [
    "Password reset requested", "Temporary password issued", "Password changed - account activated"],
    log_actions(3))
with sqlite3.connect(DB_FILE) as c:
    leaked = c.execute("SELECT COUNT(*) FROM activity_log WHERE detail LIKE ?", (f"%{temp}%",)).fetchone()[0]
check("temporary password never written to activity_log", leaked == 0)

anon.post("/api/forgot-password", json={"username": "t_secretariat"}, headers=H)
reqs = adm.get("/api/admin/password-resets").get_json()["requests"]
r = adm.post(f"/api/admin/password-resets/{reqs[0]['requestId']}/dismiss", headers=H) if reqs else None
check("dismiss closes a request without changing the password", r is not None and r.status_code == 200
      and adm.get("/api/admin/password-resets").get_json()["requests"] == []
      and login(flask_app.test_client(), "t_secretariat", PW["sec"], "BAC Secretariat").status_code == 200)

# ============================================================================================
print("=== B. Procurement CRUD (Secretariat) + C. knowledge graph ===")
deadline = time.time() + 5
while GRAPH["runs"] < 1 and time.time() < deadline:
    time.sleep(0.05)
check("catch-up sync ran at server start", GRAPH["runs"] >= 1 and "SAMPLE-2026-001" in GRAPH["refs"], GRAPH)

form = {"title": "Office chairs for the BAC room", "abc": 125000.50, "unit": "General Services Office",
        "mode": "Small Value Procurement", "category": "Goods", "timeline": "Q1 2027"}
check("member cannot create (403)", mem.post("/api/procurements", json=form, headers=H).status_code == 403)
r = sec.post("/api/procurements", json=dict(form, title=""), headers=H)
check("missing title -> 400 with field error", r.status_code == 400 and "title" in r.get_json().get("errors", {}))

r = sec.post("/api/procurements", json=form, headers=H)
body = r.get_json()
ref = (body.get("procurement") or {}).get("id")
check("create -> 201, reference SR-2026-001", r.status_code == 201 and ref == "SR-2026-001", body)
check("create response says it is in the knowledge graph", (body.get("graph") or {}).get("synced") is True, body.get("graph"))
check("new record is in the graph data, as 'encoded'", ref in GRAPH["refs"] and GRAPH["sources"].get(ref) == "encoded",
      GRAPH)
check("new record is in the registry list", any(p["id"] == ref for p in sec.get("/api/procurements").get_json()["procurements"]))
g = sec.get("/api/graph/sample").get_json()
check("GET /api/graph/sample includes it (sources sample+encoded)",
      ref in [p["id"] for p in g.get("procurements", [])] and g.get("dataSources") == ["sample", "encoded"], g.get("dataSources"))

r = sec.get(f"/api/procurements/{ref}")
detail = r.get_json()
check("read one record", r.status_code == 200 and detail.get("status") == "success", detail)
version = (detail.get("procurement") or {}).get("version") or detail.get("version")
check("read returns a version token", isinstance(version, str) and len(version) == 64, detail)
edit = dict(form, title="Ergonomic office chairs for the BAC room", abc=130000, expectedVersion=version)
r = sec.put(f"/api/procurements/{ref}", json=edit, headers=H)
body = r.get_json()
check("edit -> 200 with changed fields", r.status_code == 200 and "Title" in body.get("changed", []), body)
check("edit reached the graph", (body.get("graph") or {}).get("synced") is True, body.get("graph"))
r = sec.put(f"/api/procurements/{ref}", json=edit, headers=H)
check("stale version -> 409", r.status_code == 409 and r.get_json().get("code") == "stale", r.get_json())
r = sec.put("/api/procurements/SAMPLE-2026-001", json=dict(form, expectedVersion="0" * 64), headers=H)
check("sample record is protected from editing (409)", r.status_code == 409, r.get_json())
version = body.get("version")

r = sec.post(f"/api/procurements/{ref}/archive", json={"reason": "short", "expectedVersion": version}, headers=H)
check("archive needs a reason of 10+ characters (400)", r.status_code == 400)
r = sec.post(f"/api/procurements/{ref}/archive", json={"reason": "Duplicate entry encoded by mistake",
                                                        "expectedVersion": version}, headers=H)
check("archive -> 200", r.status_code == 200, r.get_json())
check("archived record removed from the graph", ref not in GRAPH["refs"], GRAPH["refs"])
check("archived record hidden from the registry",
      all(p["id"] != ref for p in sec.get("/api/procurements").get_json()["procurements"]))
arch = sec.get("/api/archived-procurements").get_json()
rec = next((a for a in arch.get("records", []) if a["id"] == ref), None)
check("archived list shows it with the reason", rec is not None and rec["reason"] == "Duplicate entry encoded by mistake", arch)
r = adm.post(f"/api/procurements/{ref}/restore", json={"expectedVersion": rec["version"] if rec else ""}, headers=H)
check("super admin restores -> 200", r.status_code == 200, r.get_json())
check("restored record is back in the graph", ref in GRAPH["refs"], GRAPH["refs"])

print("=== C2. Neo4j down: save still succeeds, graph catches up automatically ===")
GRAPH["fail_next"] = 1
r = sec.post("/api/procurements", json=dict(form, title="Printer toner"), headers=H)
body = r.get_json()
ref2 = (body.get("procurement") or {}).get("id")
check("create still saved (201) while the sync fails", r.status_code == 201 and ref2 == "SR-2026-002", body)
check("response says the graph update is pending", (body.get("graph") or {}).get("pending") is True, body.get("graph"))
deadline = time.time() + 5
while ref2 not in GRAPH["refs"] and time.time() < deadline:
    time.sleep(0.05)
check("worker retried and the record reached the graph", ref2 in GRAPH["refs"], GRAPH["refs"])
st = adm.get("/api/graph/neo4j/autosync").get_json()
check("autosync status: up to date, last run success", st["autosync"]["upToDate"]
      and st["autosync"]["lastStatus"] == "success", st)
check("record is still in the database (stays there)",
      any(p["id"] == ref2 for p in sec.get("/api/procurements").get_json()["procurements"]))

print(f"\nTOTAL: {sum(RESULTS)}/{len(RESULTS)} passed  (SIMULATED - SQLite stand-in for MariaDB, no Neo4j)")
sys.exit(0 if all(RESULTS) else 1)
