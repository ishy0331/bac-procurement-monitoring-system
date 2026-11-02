"""
backend/procurement_api.py - Phase 3: procurement records API.

STEP 3A (this version): Step 1 + Step 2 + update a procurement.

    GET  /api/procurements/<reference>   one City procurement record (all staff)       [Step 1]
    POST /api/procurements               create a Pre-Procurement record (Secretariat) [Step 2]
    PUT  /api/procurements/<reference>   edit an encoded Pre-Procurement record         [Step 3A]
                                         (Secretariat; needs expectedVersion from GET)

(GET /api/procurements - the list - stays in app.py, unchanged.)

Create never inserts procuring-entity data: it needs system_settings
'home_procuring_entity_id' to point at an existing procuring_entities row,
and refuses (saving nothing) until that is configured.

Rules the later steps will follow (from the approved Phase 3 plan):
  * One transaction per action, at READ COMMITTED; the procurement (or counter)
    row is locked FIRST with SELECT ... FOR UPDATE, then checked, then written.
  * On MariaDB errors 1020 (snapshot isolation conflict), 1213 (deadlock) and
    1205 (lock wait timeout) the whole action is rolled back and retried, up
    to 3 times. Nothing is ever committed after an error.
  * activity_log is only INSERTed into, inside the same transaction.
  * The browser never chooses: reference number, status, data_source, creator.
  * The lowest bid is never automatically the recommended or winning bid; only
    a BAC Member's resolution selects the recommended bid.
"""

import hashlib
import re
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from flask import Blueprint, current_app, g, jsonify, request

from auth import ROLE_LABELS, require_role
from db import get_db_connection

procurement_bp = Blueprint("procurements", __name__)

STAFF_ROLES = ("secretariat", "member", "admin")
CITY_SOURCES = ("sample", "encoded")          # PhilGEPS reference records are never City records
PRE_PROCUREMENT = "Pre-Procurement"

REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,49}$")   # e.g. SR-2026-001, SAMPLE-2026-001
MAX_AMOUNT = Decimal("9999999999999.99")      # largest value DECIMAL(15,2) can hold
AUDIT_TONES = ("info", "ok", "warn")          # activity_log.tone ENUM

RETRYABLE_ERRNOS = {1020, 1205, 1213}         # snapshot conflict, lock wait timeout, deadlock
MAX_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Errors and replies
# ---------------------------------------------------------------------------
class ValidationError(Exception):
    """Invalid input. `errors` maps a form field name to a message."""

    def __init__(self, errors):
        super().__init__("Invalid procurement data")
        self.errors = errors


class ApiError(Exception):
    """A request that cannot be completed (not found, wrong status, ...)."""

    def __init__(self, message, status, **extra):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra


def json_error(message, status, **extra):
    body = {"status": "error", "message": message}
    body.update(extra)
    return jsonify(body), status


# ---------------------------------------------------------------------------
# Validation helpers (used from Step 2 onward)
# ---------------------------------------------------------------------------
def clean_text(payload, field, errors, *, label, max_len, required=True):
    """Trim and collapse spaces. Returns the text, or None when empty/invalid."""
    value = payload.get(field)
    if value is None:
        value = ""
    if not isinstance(value, str):
        errors[field] = f"{label} must be text."
        return None
    value = " ".join(value.split())
    if not value:
        if required:
            errors[field] = f"{label} is required."
        return None
    if len(value) > max_len:
        errors[field] = f"{label} must be at most {max_len} characters."
        return None
    return value


def clean_amount(payload, field, errors, *, label, required=True):
    """Peso amount as an exact Decimal with at most 2 decimal places (> 0)."""
    raw = payload.get(field)
    if raw is None or raw == "" or isinstance(raw, bool):
        if required:
            errors[field] = f"{label} is required."
        return None
    text = str(raw).replace("₱", "").replace(",", "").strip()
    try:
        value = Decimal(text)
    except InvalidOperation:
        errors[field] = f"{label} must be a number, e.g. 4,850,000.00."
        return None
    if not value.is_finite() or value <= 0:
        errors[field] = f"{label} must be greater than zero."
        return None
    if value > MAX_AMOUNT:
        errors[field] = f"{label} is too large."
        return None
    if value != value.quantize(Decimal("0.01")):
        errors[field] = f"{label} can have at most 2 decimal places."
        return None
    return value.quantize(Decimal("0.01"))


def clean_date(payload, field, errors, *, label, required=True):
    """Date in YYYY-MM-DD form (what <input type="date"> sends)."""
    raw = payload.get(field)
    if raw is None or raw == "":
        if required:
            errors[field] = f"{label} is required."
        return None
    if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw.strip()):
        errors[field] = f"{label} must be a date (YYYY-MM-DD)."
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        errors[field] = f"{label} is not a valid date."
        return None


def normalize_key(name):
    """Same matching rule as seed.py (used for supplier / entity match_key)."""
    key = str(name or "").lower().replace("&", " and ")
    key = re.sub(r"[.,'’()#]", " ", key)
    key = re.sub(r"\b(incorporated|inc|corporation|corp|co)\b", " ", key)
    return re.sub(r"\s+", " ", key).strip()


# ---------------------------------------------------------------------------
# Write foundation (used from Step 2 onward; NOT called by any Step 1 route)
# ---------------------------------------------------------------------------
def log_activity(cur, action, detail, *, entity_id=None, entity_type="procurement", tone="info"):
    """INSERT one audit row inside the caller's transaction (never UPDATE/DELETE:
    activity_log is append-only). The actor is the signed-in user."""
    user = g.user
    actor = f"{ROLE_LABELS.get(user['role'], user['role'])} ({user['username']})"
    cur.execute("INSERT INTO activity_log (user_id, actor_label, action, detail, tone, "
                "entity_type, entity_id, ip_address) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (user["user_id"], actor[:100], action[:150], detail,
                 tone if tone in AUDIT_TONES else "info",
                 entity_type[:50] if entity_type else None, entity_id,
                 (request.remote_addr or "")[:45] or None))


def lock_procurement(cur, reference):
    """Lock one City procurement row until COMMIT/ROLLBACK and return it.
    Must be the FIRST read of the transaction (MariaDB snapshot isolation)."""
    cur.execute("SELECT procurement_id, reference_no, title, status, data_source "
                "FROM procurements WHERE reference_no = %s FOR UPDATE", (reference,))
    row = cur.fetchone()
    if row is None or row["data_source"] not in CITY_SOURCES:
        raise ApiError("Procurement not found.", 404)
    return row


def run_write(action):
    """Run action(cur) in ONE transaction and return its result.

    Commits only if action() finished without an error. Any error rolls back.
    MariaDB errors 1020 / 1205 / 1213 are retried (up to MAX_ATTEMPTS);
    ValidationError and ApiError are never retried.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        conn = None
        cur = None
        try:
            conn = get_db_connection()
            conn.autocommit = False
            cur = conn.cursor(dictionary=True, buffered=True)
            # Start clean, then run this transaction at READ COMMITTED: every plain
            # SELECT sees the latest COMMITTED data (a fresh snapshot per statement),
            # so after waiting for a FOR UPDATE lock the next reads already include
            # what the previous lock holder committed. Correctness comes from the
            # row locks, not from a snapshot. (The connection is closed afterwards.)
            conn.rollback()
            cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
            result = action(cur)
            conn.commit()
            return result
        except (ValidationError, ApiError):
            if conn is not None:
                conn.rollback()
            raise
        except Exception as exc:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    current_app.logger.exception("Rollback failed")
            if getattr(exc, "errno", None) in RETRYABLE_ERRNOS and attempt < MAX_ATTEMPTS:
                current_app.logger.warning("Retrying after database error %s (attempt %s)",
                                           getattr(exc, "errno", None), attempt)
                time.sleep(0.1 * attempt)
                continue
            raise
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:
                    pass
            if conn is not None:
                conn.close()


# ---------------------------------------------------------------------------
# Read one record (Step 1) - SELECT only
# ---------------------------------------------------------------------------
DETAIL_SQL = """
SELECT
    p.reference_no,
    p.title,
    p.end_user_unit,
    p.procurement_class,
    p.abc,
    p.target_timeline,
    p.fiscal_year,
    p.status,
    p.created_at,
    p.updated_at,
    p.updated_by,
    p.mode_id,
    p.data_source,
    m.name   AS mode_name,
    c.name   AS category_name,
    pe.name  AS entity_name,
    cu.full_name AS created_by_name,
    uu.full_name AS updated_by_name,
    (SELECT COUNT(*) FROM bid_notices n     WHERE n.procurement_id = p.procurement_id) AS notice_count,
    (SELECT COUNT(*) FROM bids b            WHERE b.procurement_id = p.procurement_id) AS bid_count,
    (SELECT COUNT(*) FROM bac_resolutions r WHERE r.procurement_id = p.procurement_id) AS resolution_count,
    (SELECT COUNT(*) FROM award_notices a   WHERE a.procurement_id = p.procurement_id) AS award_count
FROM procurements p
JOIN procurement_modes m   ON m.mode_id   = p.mode_id
JOIN procuring_entities pe ON pe.entity_id = p.procuring_entity_id
LEFT JOIN categories c     ON c.category_id = p.category_id
LEFT JOIN users cu         ON cu.user_id = p.created_by
LEFT JOIN users uu         ON uu.user_id = p.updated_by
WHERE p.reference_no = %s
  AND p.data_source IN ('sample', 'encoded')
"""


def _number(value):
    if value is None:
        return None
    return float(value) if isinstance(value, Decimal) else value


def _iso(value):
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    return str(value)


def record_version(row):
    """Version token for the stale-edit check (Step 3A).

    A hash of the editable fields, the status, updated_by AND updated_at. updated_at
    alone is not enough: it is a DATETIME with 1-second resolution, so two saves in
    the same second would look identical. Every saved edit changes at least one
    hashed field (an edit with no changes is not written), so any committed change
    gives a new token, even within the same second. No schema change is needed.
    Not a secret: it only tells the editor whether the record changed since it was read."""
    abc = row.get("abc")
    parts = [
        row.get("reference_no"), row.get("title"), row.get("end_user_unit"), row.get("procurement_class"),
        row.get("mode_id"), f"{Decimal(str(abc)):.2f}" if abc is not None else None,
        row.get("target_timeline"), row.get("status"), row.get("updated_by"), _iso(row.get("updated_at")),
    ]
    text = "\x1f".join("" if v is None else str(v) for v in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def detail_to_json(row, role):
    counts = {
        "notices": int(row["notice_count"] or 0),
        "bids": int(row["bid_count"] or 0),
        "resolutions": int(row["resolution_count"] or 0),
        "awards": int(row["award_count"] or 0),
    }
    # Step 3A decision A: only records encoded through the system can be changed;
    # sample records stay protected.
    can_edit = (role == "secretariat" and row["status"] == PRE_PROCUREMENT
                and row.get("data_source") == "encoded")
    can_delete = can_edit and not any(counts.values())
    return {
        "id": row["reference_no"],                 # the reference number (not the database ID)
        "title": row["title"],
        "unit": row["end_user_unit"],
        "category": row["procurement_class"],       # Goods / Infrastructure / Consulting Services
        "commodityCategory": row["category_name"],  # e.g. Printing Services (may be empty)
        "mode": row["mode_name"],
        "abc": _number(row["abc"]),
        "timeline": row["target_timeline"],
        "fiscalYear": row["fiscal_year"],
        "status": row["status"],
        "procuringEntity": row["entity_name"],
        "createdAt": _iso(row["created_at"]),
        "updatedAt": _iso(row["updated_at"]),
        "createdBy": row["created_by_name"],
        "updatedBy": row["updated_by_name"],
        "counts": counts,
        "canEdit": can_edit,
        "canDelete": can_delete,
        "version": record_version(row),            # send back as expectedVersion when editing
    }


@procurement_bp.route("/api/procurements/<reference>", methods=["GET"])
@require_role(*STAFF_ROLES)
def get_procurement(reference):
    """One City procurement record. Read-only: no locks, no writes."""
    if not REFERENCE_PATTERN.match(reference or ""):
        return json_error("Procurement not found.", 404)

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True, buffered=True)
        try:
            cur.execute(DETAIL_SQL, (reference,))
            row = cur.fetchone()
        finally:
            cur.close()
    except Exception:
        current_app.logger.exception("Failed to load procurement %s", reference)
        return json_error("Could not load the procurement record.", 500)
    finally:
        if conn is not None:
            conn.close()

    if row is None:
        return json_error("Procurement not found.", 404)
    return jsonify({"status": "success", "procurement": detail_to_json(row, g.user["role"])})


# ---------------------------------------------------------------------------
# Create a procurement (Step 2) - Secretariat only
# ---------------------------------------------------------------------------
PROCUREMENT_CLASSES = ("Goods", "Infrastructure", "Consulting Services")   # schema ENUM
REFERENCE_PREFIX = "SR"
FISCAL_YEAR_KEY = "active_fiscal_year"
HOME_ENTITY_KEY = "home_procuring_entity_id"
TEXT_LIMITS = {"title": 500, "unit": 150, "timeline": 100}            # column sizes
CREATE_ATTEMPTS = 2                                                    # extra try on a reference-number race
REFERENCE_UNIQUE_KEY = "uq_procurements_reference"                     # schema: UNIQUE KEY on procurements.reference_no
# MariaDB 1062 text: "Duplicate entry 'SR-2026-001' for key 'uq_procurements_reference'"
# (MySQL-style servers may write 'procurements.uq_procurements_reference').
_REFERENCE_COLLISION = re.compile(r"for key '(?:procurements\.)?" + REFERENCE_UNIQUE_KEY + r"'")


def is_reference_collision(exc):
    """True only for error 1062 on the UNIQUE key of procurements.reference_no.
    Any other duplicate key (counter row, entity, mode, ...) is NOT a reference race."""
    if getattr(exc, "errno", None) != 1062:
        return False
    message = getattr(exc, "msg", None) or str(exc)
    return bool(_REFERENCE_COLLISION.search(str(message)))


def validate_create(payload):
    """Check the form fields WITHOUT touching the database.
    Returns (cleaned values, warnings) or raises ValidationError listing every problem.
    Fields the server controls (reference, status, fiscal year, creator,
    timestamps, data source) are never taken from the browser."""
    errors = {}
    cleaned = {
        "title": clean_text(payload, "title", errors, label="Project title", max_len=TEXT_LIMITS["title"]),
        "unit": clean_text(payload, "unit", errors, label="End-user unit", max_len=TEXT_LIMITS["unit"]),
        "timeline": clean_text(payload, "timeline", errors, label="Target implementation",
                               max_len=TEXT_LIMITS["timeline"], required=False),
        "abc": clean_amount(payload, "abc", errors, label="The ABC"),
        "mode": clean_text(payload, "mode", errors, label="Mode of procurement", max_len=100),
    }
    category = payload.get("category")
    if category not in PROCUREMENT_CLASSES:
        errors["category"] = "Select Goods, Infrastructure or Consulting Services."
    cleaned["category"] = category if category in PROCUREMENT_CLASSES else None

    warnings = []
    requested_status = payload.get("status")
    if requested_status not in (None, "", PRE_PROCUREMENT):
        warnings.append("Saved as Pre-Procurement. Use Post ITB/RFQ to post the notice "
                        "(it needs the PhilGEPS reference and the deadline).")
    if errors:
        raise ValidationError(errors)
    return cleaned, warnings


def _sr_number(reference, prefix):
    """'SR-2026-014' -> 14 for prefix 'SR-2026-'; anything else -> None."""
    tail = reference[len(prefix):] if reference and reference.startswith(prefix) else ""
    return int(tail) if tail.isdigit() else None


def _create_in_transaction(cur, cleaned):
    """All database work for one create. Runs inside run_write (one transaction).

    Lock order (locking reads FIRST, as MariaDB snapshot isolation requires):
      1. the active_fiscal_year settings row  - every create waits here, one at a time
      2. this year's reference counter row     - if it already exists
    Then plain reads, then the writes. Any error rolls everything back."""
    # 1. Fiscal year - locked: serializes all creates (no duplicate numbers)
    cur.execute("SELECT setting_value FROM system_settings WHERE setting_key = %s FOR UPDATE",
                (FISCAL_YEAR_KEY,))
    row = cur.fetchone()
    fiscal_year = str(row["setting_value"]).strip() if row else ""
    if not re.fullmatch(r"\d{4}", fiscal_year):
        raise ApiError("The active fiscal year is not set. Ask the Super Admin to set it. "
                       "Nothing was saved.", 409, code="fiscal_year_not_set")

    # 2. This year's counter - locked if it exists
    counter_key = f"reference_seq_{fiscal_year}"
    cur.execute("SELECT setting_value FROM system_settings WHERE setting_key = %s FOR UPDATE",
                (counter_key,))
    counter_row = cur.fetchone()

    # 3. Home procuring entity - must already be configured (never created here)
    cur.execute("SELECT setting_value FROM system_settings WHERE setting_key = %s", (HOME_ENTITY_KEY,))
    row = cur.fetchone()
    entity_value = str(row["setting_value"]).strip() if row else ""
    entity = None
    if entity_value.isdigit():
        cur.execute("SELECT entity_id, name FROM procuring_entities WHERE entity_id = %s", (int(entity_value),))
        entity = cur.fetchone()
    if entity is None:
        raise ApiError("The City's procuring entity is not configured yet (waiting for BAC confirmation "
                       "of the official name). Nothing was saved.", 409, code="entity_not_configured")

    # 4. Mode of procurement - must be one of the seeded modes
    cur.execute("SELECT mode_id FROM procurement_modes WHERE name = %s", (cleaned["mode"],))
    row = cur.fetchone()
    if row is None:
        raise ValidationError({"mode": "Select a mode of procurement from the list."})
    mode_id = row["mode_id"]

    # 5. Next number = highest of (counter, existing SR-<year>- references) + 1
    prefix = f"{REFERENCE_PREFIX}-{fiscal_year}-"
    cur.execute("SELECT reference_no FROM procurements WHERE reference_no LIKE %s", (prefix + "%",))
    existing = [n for n in (_sr_number(r["reference_no"], prefix) for r in cur.fetchall()) if n is not None]
    counter_value = str(counter_row["setting_value"]).strip() if counter_row else "0"
    if not counter_value.isdigit():
        raise ApiError(f"The reference counter '{counter_key}' is damaged. Ask the Super Admin to check "
                       "system_settings. Nothing was saved.", 409, code="counter_invalid")
    last_used = max([int(counter_value)] + existing)
    next_number = last_used + 1
    reference_no = f"{prefix}{next_number:03d}"

    # 6. Save the counter (never goes down, so deleted drafts' numbers are never reused)
    if counter_row is None:
        cur.execute("INSERT INTO system_settings (setting_key, setting_value, updated_by) VALUES (%s, %s, %s)",
                    (counter_key, str(next_number), g.user["user_id"]))
    else:
        cur.execute("UPDATE system_settings SET setting_value = %s, updated_by = %s WHERE setting_key = %s",
                    (str(next_number), g.user["user_id"], counter_key))

    # Non-blocking warning: same title already in the City registry
    cur.execute("SELECT reference_no FROM procurements WHERE data_source IN ('sample', 'encoded') "
                "AND title = %s ORDER BY procurement_id LIMIT 5", (cleaned["title"],))
    duplicates = [r["reference_no"] for r in cur.fetchall()]

    # 7. The procurement itself - status, source, year and creator set by the server
    cur.execute("INSERT INTO procurements (reference_no, title, procuring_entity_id, end_user_unit, "
                "procurement_class, category_id, mode_id, abc, control_no, target_timeline, fiscal_year, "
                "status, data_source, created_by, updated_by) "
                "VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, NULL, %s, %s, %s, 'encoded', %s, %s)",
                (reference_no, cleaned["title"], entity["entity_id"], cleaned["unit"], cleaned["category"],
                 mode_id, cleaned["abc"], cleaned["timeline"], int(fiscal_year), PRE_PROCUREMENT,
                 g.user["user_id"], g.user["user_id"]))
    procurement_id = cur.lastrowid

    # 8. Audit row (INSERT only; same transaction, so it exists only if the create is saved)
    log_activity(cur, "Procurement created",
                 f"{reference_no} - {cleaned['title']} (ABC {cleaned['abc']:,.2f}) "
                 f"saved as {PRE_PROCUREMENT}.", entity_id=procurement_id)

    return {"reference_no": reference_no, "duplicates": duplicates, "mode": cleaned["mode"]}


def registry_record_for_new(cleaned, reference_no):
    """The new record in the same shape GET /api/procurements (registry.py) returns."""
    return {
        "id": reference_no, "title": cleaned["title"], "unit": cleaned["unit"], "mode": cleaned["mode"],
        "category": cleaned["category"], "abc": float(cleaned["abc"]), "timeline": cleaned["timeline"],
        "status": PRE_PROCUREMENT, "bidder": "", "bidAmount": None, "bids": None, "philgeps": None,
        "deadline": None, "resolution": None, "basis": None, "noa": None, "awardDate": None,
        "noticeType": None, "dataSource": "encoded",
    }


@procurement_bp.route("/api/procurements", methods=["POST"])
@require_role("secretariat")
def create_procurement():
    """Create one Pre-Procurement record. 201 on success; nothing is saved on any error."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return json_error("Request body must be JSON.", 400)
    try:
        cleaned, warnings = validate_create(payload)
    except ValidationError as exc:
        return json_error("Please correct the highlighted fields.", 400, errors=exc.errors)

    try:
        for attempt in range(1, CREATE_ATTEMPTS + 1):
            try:
                result = run_write(lambda cur: _create_in_transaction(cur, cleaned))
                break
            except Exception as exc:                       # reference-number race: try once more
                if is_reference_collision(exc) and attempt < CREATE_ATTEMPTS:
                    current_app.logger.warning("Duplicate reference number while creating; retrying once")
                    continue
                raise                                      # any other error (incl. other 1062s): not retried
    except ValidationError as exc:
        return json_error("Please correct the highlighted fields.", 400, errors=exc.errors)
    except ApiError as exc:
        return json_error(exc.message, exc.status, **exc.extra)
    except Exception as exc:
        current_app.logger.exception("Create procurement failed")
        if getattr(exc, "errno", None) in RETRYABLE_ERRNOS:
            return json_error("The system is busy. Please try again. Nothing was saved.", 503)
        return json_error("Could not save the procurement. Nothing was saved.", 500)

    if result["duplicates"]:
        warnings.append("Possible duplicate: a procurement with the same title already exists ("
                        + ", ".join(result["duplicates"]) + ").")
    return jsonify({
        "status": "success",
        "message": f"{result['reference_no']} saved as {PRE_PROCUREMENT}.",
        "procurement": registry_record_for_new(cleaned, result["reference_no"]),
        "warnings": warnings,
    }), 201


# ---------------------------------------------------------------------------
# Update a procurement (Step 3A) - Secretariat only, encoded Pre-Procurement records
# ---------------------------------------------------------------------------
VERSION_PATTERN = re.compile(r"^[0-9a-f]{64}$")
EDIT_LABELS = {"title": "Title", "unit": "End-user unit", "category": "Category",
               "mode": "Mode", "abc": "ABC", "timeline": "Target implementation"}

EDIT_ROW_SQL = ("SELECT p.procurement_id, p.reference_no, p.title, p.end_user_unit, p.procurement_class, "
                "p.mode_id, m.name AS mode_name, p.abc, p.target_timeline, p.status, p.data_source, "
                "p.updated_by, p.updated_at "
                "FROM procurements p JOIN procurement_modes m ON m.mode_id = p.mode_id "
                "WHERE p.procurement_id = %s")


def validate_update(payload):
    """Same field rules as create (validate_create is reused unchanged), plus the
    version token from GET. Protected fields in the request (reference, status,
    data_source, fiscal year, entity, creator, ...) are never read."""
    errors = {}
    try:
        cleaned, _ = validate_create(payload)          # create's status warning does not apply here
    except ValidationError as exc:
        errors.update(exc.errors)
        cleaned = None
    expected = payload.get("expectedVersion")
    if not isinstance(expected, str) or not VERSION_PATTERN.match(expected):
        errors["expectedVersion"] = "Reload the record before editing it."
    if errors:
        raise ValidationError(errors)
    return cleaned, expected


def _show(field, value):
    if value is None or value == "":
        return "(blank)"
    if field == "abc":
        return f"{Decimal(str(value)):,.2f}"
    return f"'{value}'"


def _update_in_transaction(cur, reference, cleaned, expected):
    """All database work for one edit. Runs inside run_write (one transaction).
    The procurement row is locked FIRST; every check runs on the locked row."""
    locked = lock_procurement(cur, reference)            # FOR UPDATE; 404 unless a City record
    cur.execute(EDIT_ROW_SQL, (locked["procurement_id"],))
    row = cur.fetchone()

    if row["data_source"] != "encoded":
        raise ApiError("Sample records are protected and cannot be edited. Nothing was saved.",
                       409, code="not_editable")
    if row["status"] != PRE_PROCUREMENT:
        raise ApiError(f"Only Pre-Procurement records can be edited; this record is '{row['status']}'. "
                       "Nothing was saved.", 409, code="not_editable")
    if record_version(row) != expected:
        raise ApiError("This record was changed by someone else after you opened it. Reload it and "
                       "try again. Nothing was saved.", 409, code="stale")

    cur.execute("SELECT mode_id FROM procurement_modes WHERE name = %s", (cleaned["mode"],))
    mode = cur.fetchone()
    if mode is None:
        raise ValidationError({"mode": "Select a mode of procurement from the list."})

    old = {"title": row["title"], "unit": row["end_user_unit"], "category": row["procurement_class"],
           "mode": row["mode_name"], "abc": Decimal(str(row["abc"])), "timeline": row["target_timeline"] or None}
    new = {"title": cleaned["title"], "unit": cleaned["unit"], "category": cleaned["category"],
           "mode": cleaned["mode"], "abc": cleaned["abc"], "timeline": cleaned["timeline"] or None}
    changed = [f for f in EDIT_LABELS if old[f] != new[f]]
    if not changed:                                       # nothing to save: no UPDATE, no audit row
        return {"changed": [], "row": row, "duplicates": []}

    # Only the editable columns + updated_by. reference_no, status, data_source, fiscal_year,
    # entity, category_id, control_no and created_by are never written here.
    cur.execute("UPDATE procurements SET title = %s, end_user_unit = %s, procurement_class = %s, "
                "mode_id = %s, abc = %s, target_timeline = %s, updated_by = %s WHERE procurement_id = %s",
                (new["title"], new["unit"], new["category"], mode["mode_id"], new["abc"], new["timeline"],
                 g.user["user_id"], row["procurement_id"]))

    duplicates = []
    if "title" in changed:                                # non-blocking warning, as in create
        cur.execute("SELECT reference_no FROM procurements WHERE data_source IN ('sample', 'encoded') "
                    "AND title = %s AND procurement_id <> %s ORDER BY procurement_id LIMIT 5",
                    (new["title"], row["procurement_id"]))
        duplicates = [r["reference_no"] for r in cur.fetchall()]

    log_activity(cur, "Procurement updated",
                 f"{row['reference_no']} - " + "; ".join(
                     f"{EDIT_LABELS[f]}: {_show(f, old[f])} -> {_show(f, new[f])}" for f in changed),
                 entity_id=row["procurement_id"])

    cur.execute(EDIT_ROW_SQL, (row["procurement_id"],))   # new updated_at for the new version token
    return {"changed": changed, "row": cur.fetchone(), "duplicates": duplicates}


@procurement_bp.route("/api/procurements/<reference>", methods=["PUT"])
@require_role("secretariat")
def update_procurement(reference):
    """Edit one encoded Pre-Procurement record. 200 on success; nothing is saved on any error."""
    if not REFERENCE_PATTERN.match(reference or ""):
        return json_error("Procurement not found.", 404)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return json_error("Request body must be JSON.", 400)
    try:
        cleaned, expected = validate_update(payload)
    except ValidationError as exc:
        return json_error("Please correct the highlighted fields.", 400, errors=exc.errors)

    try:
        result = run_write(lambda cur: _update_in_transaction(cur, reference, cleaned, expected))
    except ValidationError as exc:
        return json_error("Please correct the highlighted fields.", 400, errors=exc.errors)
    except ApiError as exc:
        return json_error(exc.message, exc.status, **exc.extra)
    except Exception as exc:
        current_app.logger.exception("Update procurement %s failed", reference)
        if getattr(exc, "errno", None) in RETRYABLE_ERRNOS:
            return json_error("The system is busy. Please try again. Nothing was saved.", 503)
        return json_error("Could not save the changes. Nothing was saved.", 500)

    row = result["row"]
    warnings = []
    if result["duplicates"]:
        warnings.append("Possible duplicate: a procurement with the same title already exists ("
                        + ", ".join(result["duplicates"]) + ").")
    current = {"title": row["title"], "unit": row["end_user_unit"], "mode": row["mode_name"],
               "category": row["procurement_class"], "abc": Decimal(str(row["abc"])),
               "timeline": row["target_timeline"]}
    return jsonify({
        "status": "success",
        "message": (f"{row['reference_no']} updated." if result["changed"]
                    else f"No changes to save for {row['reference_no']}."),
        "changed": [EDIT_LABELS[f] for f in result["changed"]],
        "procurement": registry_record_for_new(current, row["reference_no"]),   # Pre-Procurement draft shape
        "version": record_version(row),
        "updatedAt": _iso(row["updated_at"]),
        "warnings": warnings,
    })
