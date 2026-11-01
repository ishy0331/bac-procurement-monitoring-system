"""
backend/check_phase2_db.py - READ-ONLY compatibility check for Phase 2.

Run it on YOUR computer, against your real MariaDB, before installing Phase 2:

    cd C:\\Users\\Chad\\OneDrive\\Desktop\\ProcurementSystem_V2
    python backend\\check_phase2_db.py

It connects through your existing backend/db.py (which reads backend/config.py),
so it uses your real host, port (3307) and database. It never prints the
password or any other credential.

It only runs SELECT / SHOW statements. It does NOT insert, update, delete,
create or drop anything, and it changes no files.

Checks:
  1. connected database is procurement_db_test; server port is 3307
  2. users and activity_log columns match what backend/auth.py writes/reads
  3. the activity_log append-only triggers exist; no City procurement has
     more than one award notice
  4. the internal registry query (registry.py) runs
  5. the public registry query (public_registry.py) runs and returns only
     approved public fields
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import get_db_connection                    # your existing helper
from registry import fetch_registry
from public_registry import fetch_public_registry, NOTICE_FIELDS, AWARD_FIELDS

EXPECTED_DATABASE = "procurement_db_test"
EXPECTED_PORT = 3307

# Columns backend/auth.py uses, with the types in schema_proposal_v4.sql.
EXPECTED_COLUMNS = {
    "users": {
        "user_id": "int unsigned",
        "username": "varchar(50)",
        "full_name": "varchar(150)",
        "role": "enum('secretariat','member','admin')",
        "status": "enum('active','pending activation','disabled')",
        "password_hash": "varchar(255)",
        "failed_login_count": "tinyint unsigned",
        "locked_until": "datetime",
        "last_login_at": "datetime",
    },
    "activity_log": {
        "log_id": "bigint unsigned",
        "user_id": "int unsigned",
        "actor_label": "varchar(100)",
        "action": "varchar(150)",
        "detail": "text",
        "tone": "enum('info','ok','warn')",
        "entity_type": "varchar(50)",
        "entity_id": "int unsigned",
        "ip_address": "varchar(45)",
        "created_at": "datetime",
    },
}

RESULTS = []


def report(ok, label, detail=""):
    RESULTS.append(ok)
    print(f"  [{'OK' if ok else 'PROBLEM'}] {label}" + (f"  ({detail})" if detail else ""))


def _text(value):
    return value.decode() if isinstance(value, (bytes, bytearray)) else str(value)


def normalize_type(value):
    """'int(10) unsigned' -> 'int unsigned'; lower-case; no spaces after commas."""
    t = _text(value).lower().strip()
    t = re.sub(r"^(tinyint|smallint|int|bigint)\(\d+\)", r"\1", t)
    return re.sub(r",\s+", ",", t)


def main():
    print("Phase 2 read-only database check\n")
    try:
        conn = get_db_connection()
    except Exception as exc:
        print(f"Could not connect through backend/db.py: {type(exc).__name__}. "
              "Check that MariaDB is running and DB_CONFIG is correct.")
        return 1

    try:
        cur = conn.cursor()

        print("1. Connection")
        cur.execute("SELECT DATABASE()")
        database = cur.fetchone()[0]
        report(database == EXPECTED_DATABASE, f"database is '{EXPECTED_DATABASE}'", f"connected to '{database}'")
        if database != EXPECTED_DATABASE:
            print("\nStopped: not the test database. Nothing was read further.")
            return 1
        cur.execute("SELECT VERSION()")
        print(f"  [INFO] server version: {cur.fetchone()[0]}")
        cur.execute("SELECT @@port")
        port = cur.fetchone()[0]
        try:
            port_ok = int(_text(port)) == EXPECTED_PORT
        except ValueError:
            port_ok = False
        report(port_ok, f"server port is {EXPECTED_PORT}", f"server reports {_text(port)}")

        print("\n2. Columns used by backend/auth.py")
        for table, expected in EXPECTED_COLUMNS.items():
            cur.execute(f"SHOW COLUMNS FROM {table}")
            actual = {_text(row[0]): normalize_type(row[1]) for row in cur.fetchall()}
            for column, typ in expected.items():
                found = actual.get(column)
                report(found == typ, f"{table}.{column} is {typ}", "missing" if found is None else f"found {found}")

        print("\n3. activity_log append-only triggers")
        cur.execute("SHOW TRIGGERS LIKE 'activity_log'")
        names = {_text(row[0]) for row in cur.fetchall()}
        for trig in ("trg_activity_log_no_update", "trg_activity_log_no_delete"):
            report(trig in names, f"trigger {trig} exists")

        print("\n3b. One Notice of Award per City procurement (not enforced by the schema)")
        cur.execute("SELECT p.reference_no, COUNT(*) FROM procurements p "
                    "JOIN award_notices a ON a.procurement_id = p.procurement_id "
                    "WHERE p.data_source IN ('sample', 'encoded') "
                    "GROUP BY p.procurement_id, p.reference_no HAVING COUNT(*) > 1")
        multi = [f"{_text(r[0])} ({r[1]} notices)" for r in cur.fetchall()]
        report(not multi, "no City procurement has more than one award notice",
               "withheld from the public registry: " + ", ".join(multi) if multi else "")
        cur.close()

        print("\n4. Internal registry query (registry.py)")
        internal = fetch_registry(conn)
        report(True, "query ran", f"{len(internal)} City records")

        print("\n5. Public registry query (public_registry.py)")
        public = fetch_public_registry(conn)
        report(True, "query ran", f"{len(public)} public records: " + ", ".join(p["id"] for p in public))
        allowed = set(AWARD_FIELDS)
        extra = {k for p in public for k in p} - allowed
        report(not extra, "public records contain only approved fields", f"unexpected: {sorted(extra)}" if extra else "")
        bad = [p["id"] for p in public if p["status"] not in ("Posted (ITB/RFQ)", "Awarded")]
        report(not bad, "public records are only Posted or Awarded", ", ".join(bad))
        no_noa = [p["id"] for p in public if p["status"] == "Awarded" and not p.get("noa")]
        report(not no_noa, "every public award has a Notice of Award number", ", ".join(no_noa))
        posted_extra = [p["id"] for p in public if p["status"] != "Awarded" and set(p) - set(NOTICE_FIELDS)]
        report(not posted_extra, "posted notices carry no award fields", ", ".join(posted_extra))
    finally:
        conn.close()

    problems = RESULTS.count(False)
    print(f"\n{len(RESULTS) - problems}/{len(RESULTS)} checks OK"
          + ("" if not problems else f" - {problems} problem(s); send me the lines marked PROBLEM."))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
