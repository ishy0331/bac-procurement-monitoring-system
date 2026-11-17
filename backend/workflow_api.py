"""
backend/workflow_api.py - Procurement workflow steps saved to MariaDB.

Replaces the browser-only "demo" actions of the registry with real, audited saves:

    GET  /api/procurements/<ref>/workflow    all staff     notice, bids, resolution, award and contract
    POST /api/procurements/<ref>/notice      Secretariat   Pre-Procurement       -> Posted (ITB/RFQ)
    POST /api/procurements/<ref>/bids        Secretariat   Posted (ITB/RFQ)      -> Under Evaluation
    POST /api/procurements/<ref>/resolution  BAC Member    Under Evaluation      -> Recommended for Award
                                                                                 or Failed Bidding
    POST /api/procurements/<ref>/award       Secretariat   Recommended for Award -> Awarded
    POST /api/procurements/<ref>/contract    Secretariat   Awarded (no contract yet): record the contract

Rules (the same ones seed.py validates for the sample data):
  * One transaction per step. The procurement row is locked FIRST (SELECT ... FOR UPDATE), the status is
    checked on the locked row, then the rows are written, the status is changed and one activity_log row is
    inserted. Any error rolls everything back; deadlocks / lock timeouts are retried (procurement_api.run_write).
  * The browser never chooses the status, the awardee or the awarded amount.
  * Every bid must be within the ABC. Bids are ranked by amount (lowest = rank 1).
  * The lowest bid is NOT automatically the winner: a BAC Member's resolution picks the recommended bid
    (LCRB/SCRB need a bid from the latest notice and completed post-qualification; FAIL has no bid).
  * The Notice of Award copies the supplier and amount of the recommended bid. One award per procurement.
  * Suppliers are matched by match_key inside the procurement's own data_source (sample or encoded), so City
    records never link to PhilGEPS reference suppliers.
  * After each committed step the knowledge graph is updated (graph_autosync); a failed sync never undoes a save.
"""

from datetime import date
from decimal import Decimal

from flask import Blueprint, current_app, g, jsonify, request

from auth import require_role
from procurement_api import (ApiError, ValidationError, RETRYABLE_ERRNOS, REFERENCE_PATTERN, clean_amount,
                             clean_date, clean_text, graph_after_write, json_error, lock_procurement,
                             log_activity, normalize_key, run_write)
from db import get_db_connection

workflow_bp = Blueprint("workflow", __name__)

STAFF_ROLES = ("secretariat", "member", "admin")

PRE = "Pre-Procurement"
POSTED = "Posted (ITB/RFQ)"
EVALUATION = "Under Evaluation"
RECOMMENDED = "Recommended for Award"
AWARDED = "Awarded"
FAILED = "Failed Bidding"

NOTICE_TYPES = ("ITB", "RFQ")
RECOMMENDATIONS = ("LCRB", "SCRB", "FAIL")
BASIS_LABELS = {
    "LCRB": "Lowest Calculated Responsive Bid (LCRB)",
    "SCRB": "Single Calculated Responsive Bid (SCRB)",
    "FAIL": "Failure of Bidding",
}
MAX_BIDS = 30
REF_MAX = 50            # PhilGEPS ref, resolution no., NOA no., contract no.
SUPPLIER_MAX = 255


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _num(value):
    return float(value) if isinstance(value, Decimal) else value


def _day(value):
    if value is None:
        return None
    if hasattr(value, "date") and callable(value.date):
        value = value.date()
    return value.isoformat() if isinstance(value, date) else str(value)[:10]


def _setting(cur, key, default=None):
    cur.execute("SELECT setting_value FROM system_settings WHERE setting_key = %s", (key,))
    row = cur.fetchone()
    return str(row["setting_value"]).strip() if row and row["setting_value"] is not None else default


def _require_status(locked, allowed, step):
    if locked["status"] not in allowed:
        raise ApiError(f"{locked['reference_no']} is '{locked['status']}', so {step} is not possible. "
                       "Reload the registry. Nothing was saved.", 409, code="wrong_status",
                       currentStatus=locked["status"])


def _set_status(cur, procurement_id, status):
    cur.execute("UPDATE procurements SET status = %s, updated_by = %s, updated_at = NOW() "
                "WHERE procurement_id = %s", (status, g.user["user_id"], procurement_id))


def _procurement_extra(cur, procurement_id):
    cur.execute("SELECT p.abc, p.mode_id, m.name AS mode_name FROM procurements p "
                "JOIN procurement_modes m ON m.mode_id = p.mode_id WHERE p.procurement_id = %s",
                (procurement_id,))
    return cur.fetchone()


def _latest_notice(cur, procurement_id):
    cur.execute("SELECT notice_id, notice_type, philgeps_ref, published_date, submission_deadline, bids_received "
                "FROM bid_notices WHERE procurement_id = %s ORDER BY notice_id DESC LIMIT 1", (procurement_id,))
    return cur.fetchone()


def _latest_resolution(cur, procurement_id):
    cur.execute("SELECT resolution_id, resolution_no, recommendation, recommended_bid_id, "
                "post_qualification_done, resolution_date FROM bac_resolutions "
                "WHERE procurement_id = %s ORDER BY resolution_id DESC LIMIT 1", (procurement_id,))
    return cur.fetchone()


def _ensure_supplier(cur, name, data_source):
    """Find the supplier by match_key within the same data_source, or create it."""
    key = normalize_key(name)
    cur.execute("SELECT supplier_id, name FROM suppliers WHERE data_source = %s AND match_key = %s "
                "ORDER BY supplier_id LIMIT 1", (data_source, key))
    row = cur.fetchone()
    if row:
        return row["supplier_id"], row["name"], False
    sql = ("INSERT INTO suppliers (name, match_key, address, city, province, tin, philgeps_reg_no, "
           "data_source, verified_same_as_id) VALUES (%s, %s, %s, %s, %s, NULL, NULL, %s, NULL)")
    try:
        cur.execute(sql, (name, key, None, None, None, data_source))
    except Exception as exc:
        if getattr(exc, "errno", None) != 1048:      # "Column cannot be null": address/city/province required
            raise
        cur.execute(sql, (name, key, "", "", "", data_source))
    return cur.lastrowid, name, True


def _duplicate_check(cur, table, column, value, label):
    cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {column} = %s", (value,))
    if int(cur.fetchone()["n"]):
        raise ValidationError({column: f"{label} {value} is already used by another record."})


def _bids_of_notice(cur, notice_id):
    cur.execute("SELECT b.bid_id, b.bid_amount, b.rank_no, b.supplier_id, s.name AS supplier_name "
                "FROM bids b JOIN suppliers s ON s.supplier_id = b.supplier_id WHERE b.notice_id = %s "
                "ORDER BY b.rank_no IS NULL, b.rank_no, b.bid_amount, b.bid_id", (notice_id,))
    return cur.fetchall()


def _respond(reference, transaction, step_label, ok_status=200):
    """Common reply handling for the write endpoints."""
    if not REFERENCE_PATTERN.match(reference or ""):
        return json_error("Procurement not found.", 404)
    try:
        result = run_write(transaction)
    except ValidationError as exc:
        return json_error("Please correct the highlighted fields. Nothing was saved.", 400, errors=exc.errors)
    except ApiError as exc:
        return json_error(exc.message, exc.status, **exc.extra)
    except Exception as exc:
        current_app.logger.exception("Workflow step '%s' failed for %s", step_label, reference)
        errno = getattr(exc, "errno", None)
        if errno in RETRYABLE_ERRNOS:
            return json_error("The system is busy. Please try again. Nothing was saved.", 503)
        if errno == 1062:
            return json_error("One of the numbers you entered is already used by another record. "
                              "Nothing was saved.", 409, code="duplicate")
        return json_error(f"Could not save the {step_label}. Nothing was saved.", 500)
    graph = graph_after_write(f"{step_label} for {reference}")
    body = {"status": "success", "message": result.pop("message"), "graph": graph,
            "warnings": result.pop("warnings", [])}
    body.update(result)
    return jsonify(body), ok_status


def _payload():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValidationError({"body": "Request body must be JSON."})
    return payload


# ---------------------------------------------------------------------------
# Read: the full workflow of one procurement
# ---------------------------------------------------------------------------
def fetch_workflow(cur, reference):
    cur.execute("SELECT p.procurement_id, p.reference_no, p.title, p.status, p.abc, p.data_source, "
                "m.name AS mode_name FROM procurements p JOIN procurement_modes m ON m.mode_id = p.mode_id "
                "WHERE p.reference_no = %s AND p.data_source IN ('sample', 'encoded') AND p.archived_at IS NULL",
                (reference,))
    p = cur.fetchone()
    if p is None:
        return None
    pid = p["procurement_id"]
    notice = _latest_notice(cur, pid)
    bids = _bids_of_notice(cur, notice["notice_id"]) if notice else []
    resolution = _latest_resolution(cur, pid)
    cur.execute("SELECT a.award_id, a.notice_no, a.award_date, a.awarded_amount, a.award_basis, a.bid_id, "
                "a.contract_id, s.name AS supplier_name, c.contract_no, c.contract_date "
                "FROM award_notices a JOIN suppliers s ON s.supplier_id = a.supplier_id "
                "LEFT JOIN contracts c ON c.contract_id = a.contract_id "
                "WHERE a.procurement_id = %s ORDER BY a.award_id DESC LIMIT 1", (pid,))
    award = cur.fetchone()
    cur.execute("SELECT COUNT(*) AS n FROM bid_notices WHERE procurement_id = %s", (pid,))
    notice_count = int(cur.fetchone()["n"])
    return {
        "procurement": {"id": p["reference_no"], "title": p["title"], "status": p["status"],
                        "abc": _num(p["abc"]), "mode": p["mode_name"], "dataSource": p["data_source"],
                        "defaultNoticeType": "ITB" if p["mode_name"] == "Public Bidding" else "RFQ"},
        "notice": ({"noticeType": notice["notice_type"], "philgepsRef": notice["philgeps_ref"],
                    "publishedDate": _day(notice["published_date"]),
                    "deadline": _day(notice["submission_deadline"]), "bidsReceived": notice["bids_received"],
                    "count": notice_count} if notice else None),
        "bids": [{"bidId": b["bid_id"], "supplier": b["supplier_name"], "amount": _num(b["bid_amount"]),
                  "rank": b["rank_no"]} for b in bids],
        "resolution": ({"resolutionNo": resolution["resolution_no"],
                        "recommendation": resolution["recommendation"],
                        "basis": BASIS_LABELS.get(resolution["recommendation"]),
                        "recommendedBidId": resolution["recommended_bid_id"],
                        "postQualificationDone": bool(resolution["post_qualification_done"]),
                        "date": _day(resolution["resolution_date"])} if resolution else None),
        "award": ({"noaNo": award["notice_no"], "awardDate": _day(award["award_date"]),
                   "amount": _num(award["awarded_amount"]), "basis": award["award_basis"],
                   "supplier": award["supplier_name"], "contractNo": award["contract_no"],
                   "contractDate": _day(award["contract_date"])} if award else None),
        "requirePhilgepsRef": _setting(cur, "require_philgeps_ref", "1") == "1",
    }


@workflow_bp.route("/api/procurements/<reference>/workflow", methods=["GET"])
@require_role(*STAFF_ROLES)
def get_workflow(reference):
    if not REFERENCE_PATTERN.match(reference or ""):
        return json_error("Procurement not found.", 404)
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True, buffered=True)
        data = fetch_workflow(cur, reference)
        cur.close()
    except Exception:
        current_app.logger.exception("Could not load the workflow of %s", reference)
        return json_error("Could not load the procurement workflow.", 500)
    finally:
        if conn is not None:
            conn.close()
    if data is None:
        return json_error("Procurement not found.", 404)
    response = jsonify({"status": "success", **data})
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# 1. Post the ITB / RFQ
# ---------------------------------------------------------------------------
@workflow_bp.route("/api/procurements/<reference>/notice", methods=["POST"])
@require_role("secretariat")
def post_notice(reference):
    def tx(cur):
        payload = _payload()
        locked = lock_procurement(cur, reference)
        _require_status(locked, (PRE,), "posting the ITB/RFQ")
        errors = {}
        notice_type = payload.get("noticeType")
        if notice_type not in NOTICE_TYPES:
            errors["noticeType"] = "Select ITB or RFQ."
        ref = clean_text(payload, "philgepsRef", errors, label="PhilGEPS reference no.", max_len=REF_MAX,
                         required=False)
        published = clean_date(payload, "publishedDate", errors, label="Posting date", required=False) or date.today()
        deadline = clean_date(payload, "deadline", errors, label="Submission deadline")
        if deadline and deadline < published:
            errors["deadline"] = "The submission deadline cannot be before the posting date."
        if not ref and _setting(cur, "require_philgeps_ref", "1") == "1":
            errors["philgepsRef"] = "A PhilGEPS reference number is required (system setting)."
        if errors:
            raise ValidationError(errors)
        cur.execute("INSERT INTO bid_notices (procurement_id, notice_type, philgeps_ref, published_date, "
                    "submission_deadline, bids_received, posted_by) VALUES (%s, %s, %s, %s, %s, NULL, %s)",
                    (locked["procurement_id"], notice_type, ref, published, deadline, g.user["user_id"]))
        _set_status(cur, locked["procurement_id"], POSTED)
        log_activity(cur, f"{notice_type} posted",
                     f"{locked['reference_no']} posted ({notice_type}, PhilGEPS ref {ref or 'none'}); "
                     f"submissions until {deadline.isoformat()}.", entity_id=locked["procurement_id"])
        return {"message": f"{locked['reference_no']} is now posted (open for bidding until "
                           f"{deadline.strftime('%b %d, %Y')})."}
    return _respond(reference, tx, "ITB/RFQ posting")


# ---------------------------------------------------------------------------
# 2. Record the bids received
# ---------------------------------------------------------------------------
@workflow_bp.route("/api/procurements/<reference>/bids", methods=["POST"])
@require_role("secretariat")
def record_bids(reference):
    def tx(cur):
        payload = _payload()
        locked = lock_procurement(cur, reference)
        _require_status(locked, (POSTED,), "recording bids")
        extra = _procurement_extra(cur, locked["procurement_id"])
        abc = Decimal(str(extra["abc"]))
        raw = payload.get("bids")
        if not isinstance(raw, list) or not raw:
            raise ValidationError({"bids": "Enter at least one bid."})
        if len(raw) > MAX_BIDS:
            raise ValidationError({"bids": f"At most {MAX_BIDS} bids can be recorded at once."})
        errors, cleaned, seen = {}, [], set()
        for i, item in enumerate(raw):
            item = item if isinstance(item, dict) else {}
            e = {}
            name = clean_text(item, "supplier", e, label=f"Bidder {i + 1}", max_len=SUPPLIER_MAX)
            amount = clean_amount(item, "amount", e, label=f"Bid {i + 1} amount")
            if amount is not None and amount > abc:
                e["amount"] = f"Bid {i + 1} ({amount:,.2f}) exceeds the ABC ({abc:,.2f}) and cannot be accepted."
            if name:
                key = normalize_key(name)
                if key in seen:
                    e["supplier"] = f"Bidder {i + 1} is listed twice."
                seen.add(key)
            for field, msg in e.items():
                errors[f"bids.{i}.{field}"] = msg
            cleaned.append((name, amount))
        if errors:
            raise ValidationError(errors)
        notice = _latest_notice(cur, locked["procurement_id"])
        if notice is None:
            raise ApiError("No posted notice was found for this procurement. Nothing was saved.", 409)
        warnings = []
        if notice["submission_deadline"] and _day(notice["submission_deadline"]) > date.today().isoformat():
            warnings.append("Bids were recorded before the submission deadline "
                            f"({_day(notice['submission_deadline'])}). Check that this is intended.")
        order = sorted(range(len(cleaned)), key=lambda i: (cleaned[i][1], i))     # lowest amount = rank 1
        new_suppliers = []
        for rank, i in enumerate(order, start=1):
            name, amount = cleaned[i]
            supplier_id, stored_name, created = _ensure_supplier(cur, name, locked["data_source"])
            if created:
                new_suppliers.append(stored_name)
            cur.execute("INSERT INTO bids (procurement_id, notice_id, supplier_id, bid_amount, rank_no, recorded_by) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (locked["procurement_id"], notice["notice_id"], supplier_id, amount, rank,
                         g.user["user_id"]))
        cur.execute("UPDATE bid_notices SET bids_received = %s WHERE notice_id = %s",
                    (len(cleaned), notice["notice_id"]))
        _set_status(cur, locked["procurement_id"], EVALUATION)
        lowest = cleaned[order[0]]
        log_activity(cur, "Bids recorded",
                     f"{locked['reference_no']}: {len(cleaned)} bid(s) recorded; lowest {lowest[0]} at "
                     f"{lowest[1]:,.2f}. Forwarded to the BAC for evaluation."
                     + (f" New supplier record(s): {', '.join(new_suppliers)}." if new_suppliers else ""),
                     entity_id=locked["procurement_id"])
        return {"message": f"{len(cleaned)} bid(s) saved for {locked['reference_no']} and forwarded to the BAC.",
                "warnings": warnings, "newSuppliers": new_suppliers}
    return _respond(reference, tx, "bid record")


# ---------------------------------------------------------------------------
# 3. BAC resolution (BAC Member)
# ---------------------------------------------------------------------------
@workflow_bp.route("/api/procurements/<reference>/resolution", methods=["POST"])
@require_role("member")
def log_resolution(reference):
    def tx(cur):
        payload = _payload()
        locked = lock_procurement(cur, reference)
        _require_status(locked, (EVALUATION,), "logging a BAC resolution")
        errors = {}
        res_no = clean_text(payload, "resolutionNo", errors, label="Resolution no.", max_len=REF_MAX)
        recommendation = payload.get("recommendation")
        if recommendation not in RECOMMENDATIONS:
            errors["recommendation"] = "Select LCRB, SCRB or Failure of Bidding."
        res_date = clean_date(payload, "resolutionDate", errors, label="Resolution date", required=False) or date.today()
        if res_date > date.today():
            errors["resolutionDate"] = "The resolution date cannot be in the future."
        post_qual = payload.get("postQualificationDone") is True
        notice = _latest_notice(cur, locked["procurement_id"])
        bids = _bids_of_notice(cur, notice["notice_id"]) if notice else []
        bid = None
        if recommendation in ("LCRB", "SCRB"):
            if not post_qual:
                errors["postQualificationDone"] = "Post-qualification must be completed before recommending an award."
            wanted = payload.get("recommendedBidId")
            bid = next((b for b in bids if b["bid_id"] == wanted), None) if isinstance(wanted, int) else None
            if bid is None:
                errors["recommendedBidId"] = "Select the recommended bid from the bids of the latest notice."
        if errors:
            raise ValidationError(errors)
        if res_no:
            _duplicate_check(cur, "bac_resolutions", "resolution_no", res_no, "Resolution no.")
        cur.execute("INSERT INTO bac_resolutions (procurement_id, resolution_no, recommendation, recommended_bid_id, "
                    "post_qualification_done, resolution_date, logged_by) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (locked["procurement_id"], res_no, recommendation, bid["bid_id"] if bid else None,
                     1 if post_qual else 0, res_date, g.user["user_id"]))
        warnings = []
        if recommendation == "FAIL":
            _set_status(cur, locked["procurement_id"], FAILED)
            log_activity(cur, "Failure of bidding declared",
                         f"{locked['reference_no']} - {res_no}: failure of bidding.", entity_id=locked["procurement_id"],
                         tone="warn")
            message = f"{res_no} saved: failure of bidding declared for {locked['reference_no']}."
        else:
            _set_status(cur, locked["procurement_id"], RECOMMENDED)
            if bid["rank_no"] != 1 and recommendation == "LCRB":
                warnings.append("The recommended bid is not the lowest (rank 1). The resolution should state why "
                                "the lower bids were not responsive.")
            log_activity(cur, "BAC resolution logged",
                         f"{locked['reference_no']} - {res_no} recommends award to {bid['supplier_name']} "
                         f"({bid['bid_amount']:,.2f}, {recommendation}).", entity_id=locked["procurement_id"], tone="ok")
            message = f"{res_no} saved. {locked['reference_no']} is forwarded to the Secretariat for the Notice of Award."
        return {"message": message, "warnings": warnings}
    return _respond(reference, tx, "BAC resolution")


# ---------------------------------------------------------------------------
# 4. Notice of Award (+ optional contract)
# ---------------------------------------------------------------------------
def _clean_contract(payload, errors, required):
    contract_no = clean_text(payload, "contractNo", errors, label="Contract no.", max_len=REF_MAX, required=required)
    contract_date = clean_date(payload, "contractDate", errors, label="Contract date", required=required)
    if bool(contract_no) != bool(contract_date) and "contractNo" not in errors and "contractDate" not in errors:
        errors["contractDate" if contract_no else "contractNo"] = "Enter both the contract no. and the contract date."
    return contract_no, contract_date


def _insert_contract(cur, contract_no, contract_date):
    _duplicate_check(cur, "contracts", "contract_no", contract_no, "Contract no.")
    cur.execute("INSERT INTO contracts (contract_no, contract_date) VALUES (%s, %s)", (contract_no, contract_date))
    return cur.lastrowid


@workflow_bp.route("/api/procurements/<reference>/award", methods=["POST"])
@require_role("secretariat")
def issue_award(reference):
    def tx(cur):
        payload = _payload()
        locked = lock_procurement(cur, reference)
        _require_status(locked, (RECOMMENDED,), "issuing the Notice of Award")
        errors = {}
        noa_no = clean_text(payload, "noaNo", errors, label="Notice of Award no.", max_len=REF_MAX)
        award_date = clean_date(payload, "awardDate", errors, label="Award date")
        contract_no, contract_date = _clean_contract(payload, errors, required=False)
        resolution = _latest_resolution(cur, locked["procurement_id"])
        if resolution is None or resolution["recommendation"] not in ("LCRB", "SCRB") \
                or resolution["recommended_bid_id"] is None:
            raise ApiError("There is no BAC resolution recommending an award. Nothing was saved.", 409)
        res_day = _day(resolution["resolution_date"])
        if award_date and res_day and award_date.isoformat() < res_day:
            errors["awardDate"] = f"The award date cannot be before the BAC resolution ({res_day})."
        if award_date and award_date > date.today():
            errors["awardDate"] = "The award date cannot be in the future."
        if contract_date and award_date and contract_date < award_date:
            errors["contractDate"] = "The contract date cannot be before the award date."
        if errors:
            raise ValidationError(errors)
        cur.execute("SELECT COUNT(*) AS n FROM award_notices WHERE procurement_id = %s", (locked["procurement_id"],))
        if int(cur.fetchone()["n"]):
            raise ApiError("This procurement already has a Notice of Award. Nothing was saved.", 409)
        _duplicate_check(cur, "award_notices", "notice_no", noa_no, "Notice of Award no.")
        cur.execute("SELECT b.bid_id, b.supplier_id, b.bid_amount, s.name AS supplier_name FROM bids b "
                    "JOIN suppliers s ON s.supplier_id = b.supplier_id WHERE b.bid_id = %s",
                    (resolution["recommended_bid_id"],))
        bid = cur.fetchone()
        contract_id = _insert_contract(cur, contract_no, contract_date) if contract_no else None
        cur.execute("INSERT INTO award_notices (procurement_id, supplier_id, contract_id, bid_id, resolution_id, "
                    "notice_no, bid_reference_no, award_date, published_date, awarded_amount, award_basis, "
                    "mode_as_recorded, lot_no, lot_description, lot_quantity, certified_by_person_id, "
                    "certifier_role, issued_by) VALUES (%s, %s, %s, %s, %s, %s, NULL, %s, NULL, %s, %s, NULL, NULL, "
                    "NULL, NULL, NULL, NULL, %s)",
                    (locked["procurement_id"], bid["supplier_id"], contract_id, bid["bid_id"],
                     resolution["resolution_id"], noa_no, award_date, bid["bid_amount"],
                     BASIS_LABELS[resolution["recommendation"]], g.user["user_id"]))
        _set_status(cur, locked["procurement_id"], AWARDED)
        log_activity(cur, "Notice of Award issued",
                     f"{locked['reference_no']} - {noa_no} to {bid['supplier_name']} ({bid['bid_amount']:,.2f})"
                     + (f"; contract {contract_no} dated {contract_date.isoformat()}" if contract_no else "") + ".",
                     entity_id=locked["procurement_id"], tone="ok")
        return {"message": f"{noa_no} issued to {bid['supplier_name']}. {locked['reference_no']} is now Awarded."}
    return _respond(reference, tx, "Notice of Award")


@workflow_bp.route("/api/procurements/<reference>/contract", methods=["POST"])
@require_role("secretariat")
def record_contract(reference):
    def tx(cur):
        payload = _payload()
        locked = lock_procurement(cur, reference)
        _require_status(locked, (AWARDED,), "recording the contract")
        errors = {}
        contract_no, contract_date = _clean_contract(payload, errors, required=True)
        cur.execute("SELECT award_id, contract_id, award_date FROM award_notices WHERE procurement_id = %s "
                    "ORDER BY award_id DESC LIMIT 1", (locked["procurement_id"],))
        award = cur.fetchone()
        if award is None:
            raise ApiError("No Notice of Award was found. Nothing was saved.", 409)
        if award["contract_id"] is not None:
            raise ApiError("A contract is already recorded for this award. Nothing was saved.", 409)
        if contract_date and award["award_date"] and contract_date.isoformat() < _day(award["award_date"]):
            errors["contractDate"] = f"The contract date cannot be before the award date ({_day(award['award_date'])})."
        if errors:
            raise ValidationError(errors)
        contract_id = _insert_contract(cur, contract_no, contract_date)
        cur.execute("UPDATE award_notices SET contract_id = %s WHERE award_id = %s", (contract_id, award["award_id"]))
        cur.execute("UPDATE procurements SET updated_by = %s, updated_at = NOW() WHERE procurement_id = %s",
                    (g.user["user_id"], locked["procurement_id"]))
        log_activity(cur, "Contract recorded",
                     f"{locked['reference_no']} - contract {contract_no} dated {contract_date.isoformat()}.",
                     entity_id=locked["procurement_id"], tone="ok")
        return {"message": f"Contract {contract_no} recorded for {locked['reference_no']}."}
    return _respond(reference, tx, "contract")
