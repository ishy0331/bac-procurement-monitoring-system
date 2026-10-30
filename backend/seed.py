"""
backend/seed.py - Load FICTIONAL sample data into the TEST database.

Knowledge Graph-Based Procurement Monitoring and Supplier Visualization System
Schema: schema_proposal_v4.sql (MariaDB 10.4)

What it does
------------
* Works ONLY on the database named in TARGET_DATABASE (procurement_db_test).
  It refuses to run against procurement_db, brgy_attendance or any other name.
* Inserts lookup values, 3 demo staff accounts and 7 fictional sample
  procurements (every frontend status), with their bid notices, bids,
  BAC resolutions, award notices and contracts. All sample rows use
  data_source = 'sample', a "[SAMPLE]" title prefix and "SAMPLE-" numbers.
* Safe to re-run: every record is looked up by its natural key first and
  skipped if it already exists. Nothing is ever deleted, dropped or
  overwritten. Sample procurements that you advanced through the app are
  left exactly as they are.
* Runs as ONE transaction. Any error or failed validation check rolls back
  everything, so the database is never left half-seeded.

How to run (from the project folder, with the venv active)
-----------------------------------------------------------
    python backend\\seed.py --dry-run               (test only, saves nothing)
    python backend\\seed.py                         (insert sample data)
    python backend\\seed.py --reset-demo-passwords  (new temporary passwords
                                                    for the 3 demo accounts)
"""

import argparse
import datetime as dt
import re
import secrets
import string
import sys
from decimal import Decimal
from pathlib import Path

# Make "import db" work no matter which folder the script is started from.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from werkzeug.security import generate_password_hash  # installed with Flask

# ---------------------------------------------------------------------------
# Safety settings
# ---------------------------------------------------------------------------
TARGET_DATABASE = "procurement_db_test"
FORBIDDEN_DATABASES = {"procurement_db", "brgy_attendance"}

# Names your backend/db.py helper might use. The first one found is used.
HELPER_NAMES = ("get_connection", "get_db_connection", "get_db",
                "create_connection", "connect")

REQUIRED_TABLES = {
    "users", "procuring_entities", "categories", "procurement_modes",
    "persons", "suppliers", "supplier_representatives", "procurements",
    "bid_notices", "bids", "bac_resolutions", "contracts", "award_notices",
    "activity_log", "system_settings",
}
# Columns that only exist in schema v4 (confirms the right schema is loaded).
REQUIRED_V4_COLUMNS = {
    ("suppliers", "verified_same_as_id"),
    ("bids", "notice_id"),
    ("award_notices", "bid_id"),
}

DATA_SOURCE = "sample"
BASIS_LABELS = {
    "LCRB": "Lowest Calculated Responsive Bid (LCRB)",
    "SCRB": "Single Calculated Responsive Bid (SCRB)",
}


class SeedError(Exception):
    """Raised when a safety or validation check fails (triggers rollback)."""


# ---------------------------------------------------------------------------
# Sample data (entirely fictional)
# ---------------------------------------------------------------------------
D = dt.date
M = Decimal

PROCUREMENT_MODES = [  # names match the encoding form dropdown exactly
    ("Public Bidding", 1),
    ("Small Value Procurement (Sec. 53.9)", 0),
    ("Shopping (Sec. 52)", 0),
    ("Direct Contracting", 0),
    ("Negotiated Procurement (Emergency)", 0),
]

CATEGORIES = [
    "Construction Projects",
    "Information Technology Equipment",
    "Office Supplies",
    "Medical Supplies",
    "Furniture and Fixtures",
]

SYSTEM_SETTINGS = [  # inserted only if the key does not exist yet
    ("active_fiscal_year", "2026"),
    ("deadline_alert_days", "3"),
    ("require_philgeps_ref", "1"),
]

DEMO_USERS = [
    {"username": "demo_secretariat", "full_name": "Demo BAC Secretariat Account",
     "email": "demo.secretariat@example.invalid", "role": "secretariat"},
    {"username": "demo_member", "full_name": "Demo BAC Member Account",
     "email": "demo.member@example.invalid", "role": "member"},
    {"username": "demo_admin", "full_name": "Demo Super Admin Account",
     "email": "demo.admin@example.invalid", "role": "admin"},
]

SAMPLE_ENTITY = {"name": "Demo City Government (Sample Data)",
                 "city": "Sample City", "province": "Sample Province"}

SAMPLE_ADDRESS = "Fictional address for demonstration only"

# key -> supplier details (all data_source = 'sample')
SUPPLIERS = {
    "alpha":   {"name": "Alpha Demo Builders (Sample)",        "rep": "Demo Representative Alpha"},
    "bravo":   {"name": "Bravo Demo IT Solutions (Sample)",    "rep": "Demo Representative Bravo"},
    "charlie": {"name": "Charlie Demo Furnishings (Sample)",   "rep": "Demo Representative Charlie"},
    "delta":   {"name": "Delta Demo Medical Supply (Sample)",  "rep": "Demo Representative Delta"},
    "echo":    {"name": "Echo Demo Pharma Trading (Sample)",   "rep": "Demo Representative Echo"},
    "foxtrot": {"name": "Foxtrot Demo Construction (Sample)",  "rep": "Demo Representative Foxtrot"},
    "golf":    {"name": "Golf Demo Engineering (Sample)",      "rep": "Demo Representative Golf"},
    "hotel":   {"name": "Hotel Demo Technologies (Sample)",    "rep": "Demo Representative Hotel"},
}

# One entry per sample procurement. Bids: (supplier key, amount, rank).
# Award amounts are NOT typed here: they are copied from the recommended bid.
PROCUREMENTS = [
    {   # 1. Pre-Procurement: PR only, nothing posted yet
        "reference_no": "SAMPLE-2026-001",
        "title": "[SAMPLE] Supply of Office Laptops for Demo Records Section",
        "end_user_unit": "Demo Records Section", "procurement_class": "Goods",
        "category": "Information Technology Equipment", "mode": "Public Bidding",
        "abc": M("1500000.00"), "target_timeline": "Jan - Feb 2027",
        "status": "Pre-Procurement",
    },
    {   # 2. Posted (ITB/RFQ): RFQ open, deadline in the future, no bids yet
        "reference_no": "SAMPLE-2026-002",
        "title": "[SAMPLE] Office Supplies for the 2nd Semester",
        "end_user_unit": "Demo General Services Office", "procurement_class": "Goods",
        "category": "Office Supplies", "mode": "Small Value Procurement (Sec. 53.9)",
        "abc": M("450000.00"), "target_timeline": "Dec 2026",
        "status": "Posted (ITB/RFQ)",
        "notice": {"type": "RFQ", "philgeps_ref": "SAMPLE-REF-0002",
                   "published": D(2026, 10, 23), "deadline": D(2026, 11, 13),
                   "bids_received": None, "bids": []},
    },
    {   # 3. Under Evaluation: 3 bids received; the UI records the lowest one
        "reference_no": "SAMPLE-2026-003",
        "title": "[SAMPLE] Concrete Paving of Demo Access Road",
        "end_user_unit": "Demo Engineering Office", "procurement_class": "Infrastructure",
        "category": "Construction Projects", "mode": "Public Bidding",
        "abc": M("3800000.00"), "target_timeline": "Dec 2026 - Mar 2027",
        "status": "Under Evaluation",
        "notice": {"type": "ITB", "philgeps_ref": "SAMPLE-REF-0003",
                   "published": D(2026, 9, 28), "deadline": D(2026, 10, 19),
                   "bids_received": 3, "bids": [("alpha", M("3712400.00"), 1)]},
    },
    {   # 4. Recommended for Award: LCRB resolution, waiting for the NOA
        "reference_no": "SAMPLE-2026-004",
        "title": "[SAMPLE] Medical Supplies for Demo Health Center",
        "end_user_unit": "Demo Health Office", "procurement_class": "Goods",
        "category": "Medical Supplies", "mode": "Public Bidding",
        "abc": M("1200000.00"), "target_timeline": "Nov - Dec 2026",
        "status": "Recommended for Award",
        "notice": {"type": "ITB", "philgeps_ref": "SAMPLE-REF-0004",
                   "published": D(2026, 8, 31), "deadline": D(2026, 9, 21),
                   "bids_received": 2,
                   "bids": [("delta", M("1148000.00"), 1), ("echo", M("1176500.00"), 2)]},
        "resolution": {"no": "SAMPLE-RES-2026-004", "recommendation": "LCRB",
                       "bid": "delta", "post_qual": 1, "date": D(2026, 10, 12)},
    },
    {   # 5. Awarded (LCRB) with a contract
        "reference_no": "SAMPLE-2026-005",
        "title": "[SAMPLE] Drainage Improvement in Demo Barangay",
        "end_user_unit": "Demo Engineering Office", "procurement_class": "Infrastructure",
        "category": "Construction Projects", "mode": "Public Bidding",
        "abc": M("5500000.00"), "target_timeline": "Oct 2026 - Feb 2027",
        "status": "Awarded",
        "notice": {"type": "ITB", "philgeps_ref": "SAMPLE-REF-0005",
                   "published": D(2026, 7, 20), "deadline": D(2026, 8, 10),
                   "bids_received": 3,
                   "bids": [("alpha", M("5384250.00"), 1), ("foxtrot", M("5410000.00"), 2),
                            ("golf", M("5462900.00"), 3)]},
        "resolution": {"no": "SAMPLE-RES-2026-005", "recommendation": "LCRB",
                       "bid": "alpha", "post_qual": 1, "date": D(2026, 8, 28)},
        "award": {"notice_no": "SAMPLE-NOA-2026-005", "award_date": D(2026, 9, 7),
                  "contract_no": "SAMPLE-CON-2026-005", "contract_date": D(2026, 9, 15)},
    },
    {   # 6. Awarded (SCRB): 2 bids received, the rank-1 bid was found
        #    non-responsive, so the single responsive bid (rank 2) is awarded.
        "reference_no": "SAMPLE-2026-006",
        "title": "[SAMPLE] Network Switches and Structured Cabling",
        "end_user_unit": "Demo Information Technology Section", "procurement_class": "Goods",
        "category": "Information Technology Equipment", "mode": "Public Bidding",
        "abc": M("980000.00"), "target_timeline": "Oct - Nov 2026",
        "status": "Awarded",
        "notice": {"type": "ITB", "philgeps_ref": "SAMPLE-REF-0006",
                   "published": D(2026, 8, 3), "deadline": D(2026, 8, 24),
                   "bids_received": 2,
                   "bids": [("bravo", M("902000.00"), 1), ("hotel", M("947300.00"), 2)]},
        "resolution": {"no": "SAMPLE-RES-2026-006", "recommendation": "SCRB",
                       "bid": "hotel", "post_qual": 1, "date": D(2026, 9, 14)},
        "award": {"notice_no": "SAMPLE-NOA-2026-006", "award_date": D(2026, 9, 22),
                  "contract_no": None, "contract_date": None},
    },
    {   # 7. Failed Bidding: the only bidder failed post-qualification
        "reference_no": "SAMPLE-2026-007",
        "title": "[SAMPLE] Office Furniture for Demo Training Room",
        "end_user_unit": "Demo Human Resource Office", "procurement_class": "Goods",
        "category": "Furniture and Fixtures", "mode": "Public Bidding",
        "abc": M("650000.00"), "target_timeline": "Nov 2026",
        "status": "Failed Bidding",
        "notice": {"type": "ITB", "philgeps_ref": "SAMPLE-REF-0007",
                   "published": D(2026, 8, 17), "deadline": D(2026, 9, 7),
                   "bids_received": 1, "bids": [("charlie", M("642000.00"), 1)]},
        "resolution": {"no": "SAMPLE-RES-2026-007", "recommendation": "FAIL",
                       "bid": None, "post_qual": 1, "date": D(2026, 9, 25)},
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def normalize_key(name):
    """Same normalization rules as the frontend's normKey() (match_key)."""
    key = str(name or "").lower().replace("&", " and ")
    key = re.sub(r"[.,'’()#]", " ", key)
    key = re.sub(r"\b(incorporated|inc|corporation|corp|co)\b", " ", key)
    return re.sub(r"\s+", " ", key).strip()


PASSWORD_ALPHABET = "".join(c for c in string.ascii_letters + string.digits
                            if c not in "lIO0o1")


def make_temporary_password(length=14):
    """Random temporary password with at least one letter and one digit."""
    while True:
        pw = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))
        if any(c.isalpha() for c in pw) and any(c.isdigit() for c in pw):
            return pw


def hash_password(plain):
    return generate_password_hash(plain, method="scrypt")


def fetch_one(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchone()


def fetch_all(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchall()


class Stats:
    def __init__(self):
        self.inserted = {}
        self.skipped = {}

    def add(self, table, inserted):
        bucket = self.inserted if inserted else self.skipped
        bucket[table] = bucket.get(table, 0) + 1

    def total_inserted(self):
        return sum(self.inserted.values())


# ---------------------------------------------------------------------------
# Connection and safety checks
# ---------------------------------------------------------------------------
def open_connection():
    try:
        import db as db_helper  # backend/db.py (reads backend/config.py)
    except Exception as exc:  # noqa: BLE001 - show the real reason to the user
        raise SeedError(f"Could not import backend/db.py: {exc}") from exc

    for name in HELPER_NAMES:
        func = getattr(db_helper, name, None)
        if callable(func):
            conn = func()
            if conn is None or not hasattr(conn, "cursor") or not hasattr(conn, "commit"):
                raise SeedError(f"db.{name}() did not return a database connection. "
                                "Run backend/test_db.py to check the connection settings.")
            return conn, name

    found = [n for n in dir(db_helper) if callable(getattr(db_helper, n)) and not n.startswith("_")]
    raise SeedError("No connection function found in backend/db.py. Expected one of "
                    f"{', '.join(HELPER_NAMES)}; found: {', '.join(found) or 'none'}.")


def select_and_verify_database(cur):
    if TARGET_DATABASE in FORBIDDEN_DATABASES:
        raise SeedError("TARGET_DATABASE is set to a protected database. Aborting.")
    try:
        cur.execute(f"USE `{TARGET_DATABASE}`")  # constant above, not user input
    except Exception as exc:  # noqa: BLE001
        raise SeedError(f"Cannot open database '{TARGET_DATABASE}': {exc}") from exc

    current = fetch_one(cur, "SELECT DATABASE()")[0]
    if current != TARGET_DATABASE or current in FORBIDDEN_DATABASES:
        raise SeedError(f"Connected to '{current}', expected '{TARGET_DATABASE}'. Aborting.")

    rows = fetch_all(cur, "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS "
                          "WHERE TABLE_SCHEMA = %s", (TARGET_DATABASE,))
    tables = {r[0] for r in rows}
    columns = {(r[0], r[1]) for r in rows}
    missing_tables = REQUIRED_TABLES - tables
    if missing_tables:
        raise SeedError("Schema not loaded. Missing tables: " + ", ".join(sorted(missing_tables)))
    missing_cols = REQUIRED_V4_COLUMNS - columns
    if missing_cols:
        raise SeedError("Database does not match schema v4. Missing columns: "
                        + ", ".join(f"{t}.{c}" for t, c in sorted(missing_cols)))
    version = fetch_one(cur, "SELECT VERSION()")[0]
    return current, version


# ---------------------------------------------------------------------------
# Get-or-create functions (look up by natural key, insert only if missing)
# ---------------------------------------------------------------------------
def ensure_mode(cur, stats, name, is_competitive):
    row = fetch_one(cur, "SELECT mode_id FROM procurement_modes WHERE name = %s", (name,))
    if row:
        stats.add("procurement_modes", False)
        return row[0]
    cur.execute("INSERT INTO procurement_modes (name, is_competitive) VALUES (%s, %s)",
                (name, is_competitive))
    stats.add("procurement_modes", True)
    return cur.lastrowid


def ensure_category(cur, stats, name):
    row = fetch_one(cur, "SELECT category_id FROM categories WHERE name = %s", (name,))
    if row:
        stats.add("categories", False)
        return row[0]
    cur.execute("INSERT INTO categories (name) VALUES (%s)", (name,))
    stats.add("categories", True)
    return cur.lastrowid


def ensure_setting(cur, stats, key, value):
    row = fetch_one(cur, "SELECT setting_key FROM system_settings WHERE setting_key = %s", (key,))
    if row:  # never overwrite a value the Super Admin may have changed
        stats.add("system_settings", False)
        return
    cur.execute("INSERT INTO system_settings (setting_key, setting_value) VALUES (%s, %s)",
                (key, value))
    stats.add("system_settings", True)


def ensure_entity(cur, stats, entity):
    key = normalize_key(entity["name"])
    row = fetch_one(cur, "SELECT entity_id FROM procuring_entities WHERE match_key = %s", (key,))
    if row:
        stats.add("procuring_entities", False)
        return row[0]
    cur.execute("INSERT INTO procuring_entities (name, match_key, city, province) "
                "VALUES (%s, %s, %s, %s)",
                (entity["name"], key, entity["city"], entity["province"]))
    stats.add("procuring_entities", True)
    return cur.lastrowid


def ensure_person(cur, stats, full_name):
    key = normalize_key(full_name)
    row = fetch_one(cur, "SELECT person_id FROM persons WHERE match_key = %s", (key,))
    if row:
        stats.add("persons", False)
        return row[0]
    cur.execute("INSERT INTO persons (full_name, match_key) VALUES (%s, %s)", (full_name, key))
    stats.add("persons", True)
    return cur.lastrowid


def ensure_supplier(cur, stats, details):
    """Looks up suppliers ONLY within data_source = 'sample' (never encoded/philgeps)."""
    key = normalize_key(details["name"])
    row = fetch_one(cur, "SELECT supplier_id FROM suppliers WHERE data_source = %s AND match_key = %s",
                    (DATA_SOURCE, key))
    if row:
        stats.add("suppliers", False)
        supplier_id = row[0]
    else:
        cur.execute("INSERT INTO suppliers (name, match_key, address, city, province, tin, "
                    "philgeps_reg_no, data_source, verified_same_as_id) "
                    "VALUES (%s, %s, %s, %s, %s, NULL, NULL, %s, NULL)",
                    (details["name"], key, SAMPLE_ADDRESS, "Sample City", "Sample Province",
                     DATA_SOURCE))
        stats.add("suppliers", True)
        supplier_id = cur.lastrowid

    person_id = ensure_person(cur, stats, details["rep"])
    link = fetch_one(cur, "SELECT 1 FROM supplier_representatives "
                          "WHERE supplier_id = %s AND person_id = %s", (supplier_id, person_id))
    if link:
        stats.add("supplier_representatives", False)
    else:
        cur.execute("INSERT INTO supplier_representatives (supplier_id, person_id, position) "
                    "VALUES (%s, %s, %s)", (supplier_id, person_id, "Authorized Representative"))
        stats.add("supplier_representatives", True)
    return supplier_id


def ensure_demo_users(cur, stats):
    """Creates missing demo accounts as 'Pending activation'. Returns new passwords."""
    new_passwords = {}
    for user in DEMO_USERS:
        row = fetch_one(cur, "SELECT user_id FROM users WHERE username = %s OR email = %s",
                        (user["username"], user["email"]))
        if row:
            stats.add("users", False)
            continue
        temp_password = make_temporary_password()
        cur.execute("INSERT INTO users (username, full_name, email, password_hash, role, status, "
                    "failed_login_count, locked_until, last_login_at) "
                    "VALUES (%s, %s, %s, %s, %s, 'Pending activation', 0, NULL, NULL)",
                    (user["username"], user["full_name"], user["email"],
                     hash_password(temp_password), user["role"]))
        stats.add("users", True)
        new_passwords[user["username"]] = temp_password
    return new_passwords


def ensure_contract(cur, stats, contract_no, contract_date):
    row = fetch_one(cur, "SELECT contract_id FROM contracts WHERE contract_no = %s", (contract_no,))
    if row:
        stats.add("contracts", False)
        return row[0]
    cur.execute("INSERT INTO contracts (contract_no, contract_date) VALUES (%s, %s)",
                (contract_no, contract_date))
    stats.add("contracts", True)
    return cur.lastrowid


def seed_procurement(cur, stats, spec, entity_id, mode_ids, category_ids, supplier_ids):
    """Inserts one sample procurement and its whole chain, or skips it if it exists."""
    row = fetch_one(cur, "SELECT procurement_id, data_source FROM procurements "
                         "WHERE reference_no = %s", (spec["reference_no"],))
    if row:
        if row[1] != DATA_SOURCE:
            raise SeedError(f"{spec['reference_no']} already exists with data_source "
                            f"'{row[1]}', not 'sample'. Aborting so nothing is mixed.")
        stats.add("procurements", False)  # keep it exactly as it is (may have been
        return                            # advanced through the app)

    cur.execute("INSERT INTO procurements (reference_no, title, procuring_entity_id, end_user_unit, "
                "procurement_class, category_id, mode_id, abc, control_no, target_timeline, "
                "fiscal_year, status, data_source, created_by, updated_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, %s, %s, %s, NULL, NULL)",
                (spec["reference_no"], spec["title"], entity_id, spec["end_user_unit"],
                 spec["procurement_class"], category_ids[spec["category"]],
                 mode_ids[spec["mode"]], spec["abc"], spec["target_timeline"], 2026,
                 spec["status"], DATA_SOURCE))
    stats.add("procurements", True)
    procurement_id = cur.lastrowid

    notice = spec.get("notice")
    if not notice:
        return

    cur.execute("INSERT INTO bid_notices (procurement_id, notice_type, philgeps_ref, published_date, "
                "submission_deadline, bids_received, posted_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, NULL)",
                (procurement_id, notice["type"], notice["philgeps_ref"], notice["published"],
                 notice["deadline"], notice["bids_received"]))
    stats.add("bid_notices", True)
    notice_id = cur.lastrowid

    bids = {}  # supplier key -> (bid_id, supplier_id, amount)
    for supplier_key, amount, rank in notice["bids"]:
        if amount > spec["abc"]:
            raise SeedError(f"{spec['reference_no']}: bid {amount} exceeds ABC {spec['abc']}.")
        supplier_id = supplier_ids[supplier_key]
        cur.execute("INSERT INTO bids (procurement_id, notice_id, supplier_id, bid_amount, "
                    "rank_no, recorded_by) VALUES (%s, %s, %s, %s, %s, NULL)",
                    (procurement_id, notice_id, supplier_id, amount, rank))
        stats.add("bids", True)
        bids[supplier_key] = (cur.lastrowid, supplier_id, amount)

    resolution = spec.get("resolution")
    if not resolution:
        return

    recommended = bids[resolution["bid"]] if resolution["bid"] else None
    cur.execute("INSERT INTO bac_resolutions (procurement_id, resolution_no, recommendation, "
                "recommended_bid_id, post_qualification_done, resolution_date, logged_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, NULL)",
                (procurement_id, resolution["no"], resolution["recommendation"],
                 recommended[0] if recommended else None, resolution["post_qual"],
                 resolution["date"]))
    stats.add("bac_resolutions", True)
    resolution_id = cur.lastrowid

    award = spec.get("award")
    if not award:
        return
    if recommended is None:
        raise SeedError(f"{spec['reference_no']}: cannot award without a recommended bid.")

    contract_id = None
    if award["contract_no"]:
        contract_id = ensure_contract(cur, stats, award["contract_no"], award["contract_date"])

    bid_id, supplier_id, amount = recommended  # award copies the winning bid
    cur.execute("INSERT INTO award_notices (procurement_id, supplier_id, contract_id, bid_id, "
                "resolution_id, notice_no, bid_reference_no, award_date, published_date, "
                "awarded_amount, award_basis, mode_as_recorded, lot_no, lot_description, "
                "lot_quantity, certified_by_person_id, certifier_role, issued_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, NULL, %s, NULL, %s, %s, NULL, NULL, NULL, "
                "NULL, NULL, NULL, NULL)",
                (procurement_id, supplier_id, contract_id, bid_id, resolution_id,
                 award["notice_no"], award["award_date"], amount,
                 BASIS_LABELS[resolution["recommendation"]]))
    stats.add("award_notices", True)


# ---------------------------------------------------------------------------
# Validation checks (run before COMMIT; any failure rolls everything back)
# ---------------------------------------------------------------------------
CHECKS = [
    ("Supplier data_source matches the procurement's data_source (bids and awards)", """
        SELECT CONCAT('bid ', b.bid_id) FROM bids b
          JOIN procurements p ON p.procurement_id = b.procurement_id
          JOIN suppliers s    ON s.supplier_id    = b.supplier_id
         WHERE s.data_source <> p.data_source
        UNION ALL
        SELECT CONCAT('award ', a.award_id) FROM award_notices a
          JOIN procurements p ON p.procurement_id = a.procurement_id
          JOIN suppliers s    ON s.supplier_id    = a.supplier_id
         WHERE s.data_source <> p.data_source"""),
    ("Every bid belongs to a notice of the same procurement", """
        SELECT CONCAT('bid ', b.bid_id) FROM bids b
          JOIN bid_notices n ON n.notice_id = b.notice_id
         WHERE n.procurement_id <> b.procurement_id"""),
    ("Every bid is within the procurement ABC", """
        SELECT CONCAT('bid ', b.bid_id) FROM bids b
          JOIN procurements p ON p.procurement_id = b.procurement_id
         WHERE b.bid_amount > p.abc"""),
    ("Bids received is not less than the bids recorded for each notice", """
        SELECT CONCAT('notice ', n.notice_id) FROM bid_notices n
          JOIN bids b ON b.notice_id = n.notice_id
         GROUP BY n.notice_id, n.bids_received
        HAVING n.bids_received IS NULL OR n.bids_received < COUNT(*)"""),
    ("LCRB/SCRB resolutions have a bid from the latest notice and post-qualification; "
     "FAIL resolutions have no bid", """
        SELECT CONCAT('resolution ', r.resolution_id) FROM bac_resolutions r
          LEFT JOIN bids b ON b.bid_id = r.recommended_bid_id
         WHERE (r.recommendation = 'FAIL' AND r.recommended_bid_id IS NOT NULL)
            OR (r.recommendation IN ('LCRB', 'SCRB') AND (
                    r.recommended_bid_id IS NULL
                 OR r.post_qualification_done <> 1
                 OR b.procurement_id <> r.procurement_id
                 OR b.notice_id <> (SELECT MAX(n.notice_id) FROM bid_notices n
                                     WHERE n.procurement_id = r.procurement_id)))"""),
    ("Awards with a bid: same supplier, amount = winning bid, bid = the resolution's "
     "recommended bid, resolution is LCRB/SCRB", """
        SELECT CONCAT('award ', a.award_id) FROM award_notices a
          JOIN bids b ON b.bid_id = a.bid_id
          LEFT JOIN bac_resolutions r ON r.resolution_id = a.resolution_id
         WHERE a.supplier_id <> b.supplier_id
            OR a.awarded_amount <> b.bid_amount
            OR r.resolution_id IS NULL
            OR r.recommended_bid_id <> a.bid_id
            OR r.recommendation NOT IN ('LCRB', 'SCRB')"""),
    ("Total awarded per procurement does not exceed the ABC", """
        SELECT CONCAT('procurement ', p.procurement_id) FROM procurements p
          JOIN award_notices a ON a.procurement_id = p.procurement_id
         GROUP BY p.procurement_id, p.abc
        HAVING SUM(a.awarded_amount) > p.abc"""),
    ("Sample awards always carry their bid and resolution", """
        SELECT CONCAT('award ', a.award_id) FROM award_notices a
          JOIN procurements p ON p.procurement_id = a.procurement_id
         WHERE p.data_source = 'sample' AND (a.bid_id IS NULL OR a.resolution_id IS NULL)"""),
]


def check_status_consistency(cur):
    """Each sample procurement's status must match the records that exist for it."""
    problems = []
    rows = fetch_all(cur, """
        SELECT p.reference_no, p.status,
               (SELECT COUNT(*) FROM bid_notices n WHERE n.procurement_id = p.procurement_id),
               (SELECT COUNT(*) FROM bids b WHERE b.procurement_id = p.procurement_id),
               (SELECT r.recommendation FROM bac_resolutions r
                 WHERE r.procurement_id = p.procurement_id
                 ORDER BY r.resolution_id DESC LIMIT 1),
               (SELECT COUNT(*) FROM award_notices a WHERE a.procurement_id = p.procurement_id)
          FROM procurements p
         WHERE p.data_source = 'sample'""")
    for ref, status, notices, bids, last_rec, awards in rows:
        ok = {
            "Pre-Procurement": notices == 0,
            "Posted (ITB/RFQ)": notices >= 1 and bids == 0 and last_rec is None,
            "Under Evaluation": bids >= 1 and last_rec is None and awards == 0,
            "Recommended for Award": last_rec in ("LCRB", "SCRB") and awards == 0,
            "Awarded": last_rec in ("LCRB", "SCRB") and awards >= 1,
            "Failed Bidding": last_rec == "FAIL" and awards == 0,
        }.get(status, False)
        if not ok:
            problems.append(f"{ref} ({status}): notices={notices}, bids={bids}, "
                            f"latest resolution={last_rec}, awards={awards}")
    return rows, problems


def run_validations(cur):
    print("\nValidation checks:")
    failures = []
    for label, sql in CHECKS:
        bad = [r[0] for r in fetch_all(cur, sql)]
        print(f"  [{'PASS' if not bad else 'FAIL'}] {label}")
        if bad:
            failures.append(f"{label}: {', '.join(map(str, bad))}")

    rows, problems = check_status_consistency(cur)
    print(f"  [{'PASS' if not problems else 'FAIL'}] Sample procurement statuses match their records")
    failures.extend(problems)

    statuses = sorted({r[1] for r in rows})
    print(f"  [INFO] Sample statuses present: {', '.join(statuses) if statuses else 'none'}")
    if failures:
        raise SeedError("Validation failed:\n  - " + "\n  - ".join(failures))


# ---------------------------------------------------------------------------
# Main routines
# ---------------------------------------------------------------------------
def seed_all(cur, stats):
    mode_ids = {name: ensure_mode(cur, stats, name, comp) for name, comp in PROCUREMENT_MODES}
    category_ids = {name: ensure_category(cur, stats, name) for name in CATEGORIES}
    for key, value in SYSTEM_SETTINGS:
        ensure_setting(cur, stats, key, value)
    new_passwords = ensure_demo_users(cur, stats)
    entity_id = ensure_entity(cur, stats, SAMPLE_ENTITY)
    supplier_ids = {k: ensure_supplier(cur, stats, v) for k, v in SUPPLIERS.items()}
    for spec in PROCUREMENTS:
        seed_procurement(cur, stats, spec, entity_id, mode_ids, category_ids, supplier_ids)
    return new_passwords


def reset_demo_passwords(cur):
    new_passwords = {}
    for user in DEMO_USERS:
        row = fetch_one(cur, "SELECT user_id FROM users WHERE username = %s", (user["username"],))
        if not row:
            raise SeedError(f"Demo account '{user['username']}' does not exist. Run seed.py first.")
        temp_password = make_temporary_password()
        cur.execute("UPDATE users SET password_hash = %s, status = 'Pending activation', "
                    "failed_login_count = 0, locked_until = NULL WHERE user_id = %s",
                    (hash_password(temp_password), row[0]))
        new_passwords[user["username"]] = temp_password
    cur.execute("INSERT INTO activity_log (user_id, actor_label, action, detail, tone) "
                "VALUES (NULL, 'Seed script', 'Demo passwords reset', %s, 'warn')",
                ("Temporary passwords reissued for the 3 demo accounts "
                 "(demo_secretariat, demo_member, demo_admin).",))
    return new_passwords


def print_passwords(passwords):
    if not passwords:
        return
    print("\n" + "=" * 68)
    print(" TEMPORARY PASSWORDS - shown ONCE, not stored anywhere in plain text")
    print(" Copy them now. Each user must set a new password at first login.")
    print("=" * 68)
    for user in DEMO_USERS:
        if user["username"] in passwords:
            print(f"  {user['username']:<18} ({user['role']:<11})  {passwords[user['username']]}")
    print("=" * 68)


def main():
    parser = argparse.ArgumentParser(description="Load fictional sample data into "
                                                 f"{TARGET_DATABASE}.")
    parser.add_argument("--dry-run", action="store_true",
                        help="run everything and all checks, then roll back (saves nothing)")
    parser.add_argument("--reset-demo-passwords", action="store_true",
                        help="issue new temporary passwords for the 3 demo accounts")
    args = parser.parse_args()

    conn = None
    try:
        conn, helper = open_connection()
        conn.autocommit = False
        cur = conn.cursor(buffered=True)
        database, version = select_and_verify_database(cur)
        conn.rollback()  # start from a clean transaction
        print(f"Connected with db.{helper}() to '{database}' (server {version}).")

        stats = Stats()
        if args.reset_demo_passwords:
            passwords = reset_demo_passwords(cur)
        else:
            passwords = seed_all(cur, stats)
            run_validations(cur)
            if stats.total_inserted():
                cur.execute("INSERT INTO activity_log (user_id, actor_label, action, detail, tone) "
                            "VALUES (NULL, 'Seed script', 'Sample data seeded', %s, 'info')",
                            ("Fictional sample records inserted: " + ", ".join(
                                f"{t}={n}" for t, n in sorted(stats.inserted.items())),))

        if args.dry_run:
            conn.rollback()
            print("\nDRY RUN: all inserts and checks succeeded, then everything was rolled back. "
                  "Nothing was saved.")
        else:
            conn.commit()
            print("\nCOMMITTED.")

        if not args.reset_demo_passwords:
            print("\nRecords inserted: " + (", ".join(f"{t}={n}" for t, n in sorted(stats.inserted.items()))
                                            or "none (everything already present)"))
            print("Records already present (skipped): "
                  + (", ".join(f"{t}={n}" for t, n in sorted(stats.skipped.items())) or "none"))

        if not args.dry_run:
            print_passwords(passwords)
        elif passwords:
            print("\nDRY RUN: demo accounts would be created; no passwords were issued.")
        return 0

    except SeedError as exc:
        if conn is not None:
            conn.rollback()
        print(f"\nSTOPPED - nothing was saved.\n{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - always roll back, then show the error
        if conn is not None:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
        print(f"\nERROR - everything was rolled back, nothing was saved.\n"
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    sys.exit(main())
