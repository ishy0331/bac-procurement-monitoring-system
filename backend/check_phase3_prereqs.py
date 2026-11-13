"""
backend/check_phase3_prereqs.py - READ-ONLY checks before Phase 3 writes.

Run it on YOUR computer, against your real MariaDB:

    cd C:\\Users\\Chad\\OneDrive\\Desktop\\ProcurementSystem_V2
    python backend\\check_phase3_prereqs.py

It connects through your existing backend/db.py (backend/config.py), so it
uses your real host, port and database. It never prints a password.

It only runs SELECT / SHOW statements. It does NOT insert, update, delete,
create or drop anything, and it changes no files.

[OK] / [PROBLEM] lines are pass/fail checks.
[INFO] lines are facts for review (e.g. connection settings) - not failures.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import get_db_connection                    # your existing helper

EXPECTED_DATABASE = "procurement_db_frontend_test"
# The port must match DB_CONFIG in backend/config.py (laptop: 3306).
try:
    from config import DB_CONFIG as _DB_CONFIG
    EXPECTED_PORT = int(_DB_CONFIG.get("port", 3306))
except Exception:  # config.py missing or unreadable; the connection step reports it
    EXPECTED_PORT = 3306

# Columns Phase 3 will read or write (schema_proposal_v4.sql).
REQUIRED_COLUMNS = {
    "procurements": ["procurement_id", "reference_no", "title", "procuring_entity_id", "end_user_unit",
                     "procurement_class", "category_id", "mode_id", "abc", "target_timeline", "fiscal_year",
                     "status", "data_source", "created_by", "updated_by", "created_at", "updated_at"],
    "procuring_entities": ["entity_id", "name", "match_key", "city", "province"],
    "bid_notices": ["notice_id", "procurement_id", "notice_type", "philgeps_ref", "published_date",
                    "submission_deadline", "bids_received", "posted_by"],
    "suppliers": ["supplier_id", "name", "match_key", "data_source"],
    "bids": ["bid_id", "procurement_id", "notice_id", "supplier_id", "bid_amount", "rank_no", "recorded_by"],
    "bac_resolutions": ["resolution_id", "procurement_id", "resolution_no", "recommendation",
                        "recommended_bid_id", "post_qualification_done", "resolution_date", "logged_by"],
    "award_notices": ["award_id", "procurement_id", "supplier_id", "bid_id", "resolution_id", "notice_no",
                      "award_date", "awarded_amount", "issued_by", "lot_no"],
    "activity_log": ["log_id", "user_id", "actor_label", "action", "detail", "tone", "entity_type",
                     "entity_id", "ip_address", "created_at"],
    "system_settings": ["setting_key", "setting_value"],
}

# Tables whose rows point at procurements; deleting a procurement with such rows must be blocked.
CHILD_TABLES = ("bid_notices", "bids", "bac_resolutions", "award_notices")

RESULTS = []


def report(ok, label, detail=""):
    RESULTS.append(ok)
    print(f"  [{'OK' if ok else 'PROBLEM'}] {label}" + (f"  ({detail})" if detail else ""))


def info(label, value=""):
    print(f"  [INFO] {label}" + (f": {value}" if value != "" else ""))


def _text(value):
    return value.decode() if isinstance(value, (bytes, bytearray)) else str(value)


def first_variable(cur, names):
    """Return (name, value) for the first server variable that exists, else (None, None)."""
    for name in names:
        try:
            cur.execute(f"SELECT @@{name}")
            return name, _text(cur.fetchone()[0])
        except Exception:
            continue
    return None, None


def main():
    print("Phase 3 read-only prerequisite check\n")
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
        database = _text(cur.fetchone()[0])
        report(database == EXPECTED_DATABASE, f"database is '{EXPECTED_DATABASE}'", f"connected to '{database}'")
        if database != EXPECTED_DATABASE:
            print("\nStopped: not the test database. Nothing was read further.")
            return 1
        cur.execute("SELECT @@port")
        port = _text(cur.fetchone()[0])
        report(port == str(EXPECTED_PORT), f"server port is {EXPECTED_PORT}", f"server reports {port}")
        cur.execute("SELECT VERSION()")
        info("server version", _text(cur.fetchone()[0]))

        print("\n2. Connection settings (facts for review)")
        name, value = first_variable(cur, ["autocommit"])
        info("autocommit for connections from db.py", f"{value} (Phase 3 turns it off for each save)")
        name, value = first_variable(cur, ["transaction_isolation", "tx_isolation"])
        info("transaction isolation", f"{value} ({name})" if name else "not reported")
        name, value = first_variable(cur, ["innodb_snapshot_isolation"])
        if name:
            info("innodb_snapshot_isolation", f"{value} (ON means conflicting saves may get error 1020; "
                                               "Phase 3 retries them)")
        else:
            info("innodb_snapshot_isolation", "not available on this server")
        name, value = first_variable(cur, ["innodb_lock_wait_timeout"])
        info("innodb_lock_wait_timeout (seconds)", value)
        name, sql_mode = first_variable(cur, ["sql_mode"])
        strict = bool(sql_mode) and ("STRICT_TRANS_TABLES" in sql_mode or "STRICT_ALL_TABLES" in sql_mode)
        report(strict, "strict SQL mode is on (bad dates / too-long text are errors, not silently changed)",
               sql_mode or "not reported")

        print("\n3. Columns Phase 3 will use")
        for table, columns in REQUIRED_COLUMNS.items():
            try:
                cur.execute(f"SHOW COLUMNS FROM {table}")
                present = {_text(r[0]) for r in cur.fetchall()}
            except Exception as exc:
                report(False, f"table {table} exists", type(exc).__name__)
                continue
            missing = [c for c in columns if c not in present]
            report(not missing, f"{table}: {len(columns)} columns present",
                   "missing: " + ", ".join(missing) if missing else "")

        print("\n4. Deleting a procurement with related rows is blocked by the database")
        cur.execute("SELECT TABLE_NAME, CONSTRAINT_NAME, DELETE_RULE "
                    "FROM information_schema.REFERENTIAL_CONSTRAINTS "
                    "WHERE CONSTRAINT_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME = 'procurements'")
        rules = {}
        for table, constraint, rule in cur.fetchall():
            rules.setdefault(_text(table), []).append((_text(constraint), _text(rule)))
        for table in CHILD_TABLES:
            found = rules.get(table, [])
            ok = bool(found) and all(rule in ("RESTRICT", "NO ACTION") for _, rule in found)
            report(ok, f"{table} -> procurements foreign key blocks deletes",
                   ", ".join(f"{c}: {r}" for c, r in found) if found else "no foreign key found")

        print("\n5. activity_log is append-only")
        cur.execute("SHOW TRIGGERS LIKE 'activity_log'")
        triggers = {_text(r[0]) for r in cur.fetchall()}
        for trig in ("trg_activity_log_no_update", "trg_activity_log_no_delete"):
            report(trig in triggers, f"trigger {trig} exists")

        print("\n6. System settings")
        cur.execute("SELECT setting_key, setting_value FROM system_settings ORDER BY setting_key")
        settings = {_text(k): _text(v) for k, v in cur.fetchall()}
        fiscal_year = settings.get("active_fiscal_year")
        report(bool(fiscal_year) and bool(re.fullmatch(r"\d{4}", fiscal_year)),
               "active_fiscal_year is set to a 4-digit year", fiscal_year or "missing")
        if fiscal_year:
            key = f"reference_seq_{fiscal_year}"
            info(f"reference counter '{key}'",
                 settings[key] if key in settings else "not created yet (expected; Step 2 creates it safely)")
        info("home_procuring_entity_id",
             settings.get("home_procuring_entity_id", "not configured yet (expected; waits for BAC confirmation)"))

        print("\n7. Procuring entities (check the City's official name before setup)")
        cur.execute("SELECT pe.entity_id, pe.name, pe.city, pe.province, "
                    "(SELECT COUNT(*) FROM procurements p WHERE p.procuring_entity_id = pe.entity_id) "
                    "FROM procuring_entities pe ORDER BY pe.entity_id")
        rows = cur.fetchall()
        if not rows:
            info("procuring_entities", "no rows")
        for entity_id, name, city, province, n in rows:
            info(f"entity {entity_id}", f"{_text(name)} | {_text(city) if city else '-'} | "
                                        f"{_text(province) if province else '-'} | {n} procurement(s)")

        print("\n8. Existing references and awards")
        cur.execute("SELECT reference_no FROM procurements WHERE reference_no LIKE 'SR-%' ORDER BY reference_no")
        sr_refs = [_text(r[0]) for r in cur.fetchall()]
        info("existing SR- references", ", ".join(sr_refs) if sr_refs else "none")
        cur.execute("SELECT COUNT(*), SUM(data_source IN ('sample', 'encoded')) FROM procurements")
        total, city = cur.fetchone()
        info("procurements", f"{total} total, {int(city or 0)} City records (sample + encoded)")
        cur.execute("SELECT p.reference_no, COUNT(*) FROM procurements p "
                    "JOIN award_notices a ON a.procurement_id = p.procurement_id "
                    "WHERE p.data_source IN ('sample', 'encoded') "
                    "GROUP BY p.procurement_id, p.reference_no HAVING COUNT(*) > 1")
        multi = [f"{_text(r[0])} ({r[1]})" for r in cur.fetchall()]
        report(not multi, "no City procurement has more than one award notice", ", ".join(multi))
        cur.close()
    finally:
        conn.close()

    problems = RESULTS.count(False)
    print(f"\n{len(RESULTS) - problems}/{len(RESULTS)} checks OK"
          + ("" if not problems else f" - {problems} problem(s); send me the lines marked PROBLEM."))
    print("Nothing was changed. Please send the full output (it contains no passwords).")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
