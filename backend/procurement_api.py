"""
backend/procurement_api.py - Phase 3: procurement records API.

STEP 1 (this version): shared foundation + ONE read-only route.

    GET /api/procurements/<reference>   one City procurement record (all staff)

Step 1 writes NOTHING to the database. The write helpers below (run_write,
log_activity, lock_procurement) are the foundation for Steps 2-9; no route
calls them yet.

Rules the later steps will follow (from the approved Phase 3 plan):
  * One transaction per action; the procurement (or counter) row is locked
    FIRST with SELECT ... FOR UPDATE, then checked, then written.
  * On MariaDB errors 1020 (snapshot isolation conflict), 1213 (deadlock) and
    1205 (lock wait timeout) the whole action is rolled back and retried, up
    to 3 times. Nothing is ever committed after an error.
  * activity_log is only INSERTed into, inside the same transaction.
  * The browser never chooses: reference number, status, data_source, creator.
  * The lowest bid is never automatically the recommended or winning bid; only
    a BAC Member's resolution selects the recommended bid.
"""

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


def detail_to_json(row, role):
    counts = {
        "notices": int(row["notice_count"] or 0),
        "bids": int(row["bid_count"] or 0),
        "resolutions": int(row["resolution_count"] or 0),
        "awards": int(row["award_count"] or 0),
    }
    can_edit = role == "secretariat" and row["status"] == PRE_PROCUREMENT
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
