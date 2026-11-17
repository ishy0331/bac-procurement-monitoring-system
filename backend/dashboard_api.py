"""
backend/dashboard_api.py - Executive dashboard computed from the database (READ-ONLY).

    GET /api/dashboard     signed-in BAC staff

Everything is calculated from the City records (data_source 'sample' or 'encoded', not archived).
PhilGEPS reference records of other agencies are never counted. SELECT statements only.

Returned blocks
  kpis         tracked procurements, total ABC, awarded value, savings vs ABC, average bids per notice
  stages       count per status
  timeliness   average days per stage and items that need follow-up (system rules, see FOLLOW_UP_RULES)
  suppliers    top suppliers by awarded value (share of the total) = supplier concentration
  flags        indicators FOR REVIEW (single bid, award not to the lowest bid, high supplier share,
               bidders in one procurement sharing a representative). They are not findings of wrongdoing.
  health       data completeness / traceability percentages
  monthly      awards per month (count and value)
  recent       latest activity_log entries
"""

from collections import defaultdict
from datetime import date, datetime

from flask import Blueprint, current_app, jsonify

from auth import require_role
from db import get_db_connection

dashboard_bp = Blueprint("dashboard", __name__)

STAFF_ROLES = ("secretariat", "member", "admin")
STATUSES = ["Pre-Procurement", "Posted (ITB/RFQ)", "Under Evaluation", "Recommended for Award",
            "Awarded", "Failed Bidding"]

# Follow-up rules used by the dashboard. These are the system's own monitoring thresholds (days), NOT the
# statutory periods of RA 9184 / RA 12009; the BAC can change them under System Administration.
FOLLOW_UP_DEFAULTS = {
    "followup_bids_days": 2,          # deadline passed, bids not yet recorded
    "followup_resolution_days": 15,   # bids recorded (deadline passed), no BAC resolution yet
    "followup_award_days": 15,        # resolution recommends award, no Notice of Award yet
    "followup_contract_days": 10,     # Notice of Award issued, no contract recorded yet
}
HIGH_SHARE = 0.40                     # a supplier with >= 40% of the awarded value is flagged for review


def _f(value):
    return float(value) if value is not None else 0.0


def _d(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _days(a, b):
    a, b = _d(a), _d(b)
    return (b - a).days if a and b else None


def _avg(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 1) if values else None


def _settings(cur):
    cur.execute("SELECT setting_key, setting_value FROM system_settings")
    return {r["setting_key"]: r["setting_value"] for r in cur.fetchall()}


def _int_setting(settings, key, default):
    try:
        value = int(str(settings.get(key, default)).strip())
        return value if 0 <= value <= 365 else default
    except (TypeError, ValueError):
        return default


def build_dashboard(cur, today=None):
    today = today or date.today()
    settings = _settings(cur)
    rules = {k: _int_setting(settings, k, v) for k, v in FOLLOW_UP_DEFAULTS.items()}
    alert_days = _int_setting(settings, "deadline_alert_days", 3)

    cur.execute("SELECT procurement_id, reference_no, title, status, abc, data_source, created_at "
                "FROM procurements WHERE data_source IN ('sample', 'encoded') AND archived_at IS NULL")
    procs = {p["procurement_id"]: p for p in cur.fetchall()}
    ids = list(procs)

    def rows(sql):
        if not ids:
            return []
        cur.execute(sql.format(ids=", ".join(["%s"] * len(ids))), tuple(ids))
        return cur.fetchall()

    notices = rows("SELECT notice_id, procurement_id, published_date, submission_deadline, bids_received "
                   "FROM bid_notices WHERE procurement_id IN ({ids}) ORDER BY notice_id")
    bids = rows("SELECT b.bid_id, b.procurement_id, b.notice_id, b.supplier_id, b.bid_amount, b.rank_no, "
                "s.name AS supplier_name FROM bids b JOIN suppliers s ON s.supplier_id = b.supplier_id "
                "WHERE b.procurement_id IN ({ids})")
    resolutions = rows("SELECT resolution_id, procurement_id, resolution_no, recommendation, recommended_bid_id, "
                       "resolution_date FROM bac_resolutions WHERE procurement_id IN ({ids}) ORDER BY resolution_id")
    awards = rows("SELECT a.award_id, a.procurement_id, a.supplier_id, a.bid_id, a.resolution_id, a.contract_id, "
                  "a.notice_no, a.award_date, a.awarded_amount, s.name AS supplier_name, c.contract_date "
                  "FROM award_notices a JOIN suppliers s ON s.supplier_id = a.supplier_id "
                  "LEFT JOIN contracts c ON c.contract_id = a.contract_id "
                  "WHERE a.procurement_id IN ({ids}) ORDER BY a.award_id")

    latest_notice, latest_res, latest_award = {}, {}, {}
    for n in notices:
        latest_notice[n["procurement_id"]] = n
    for r in resolutions:
        latest_res[r["procurement_id"]] = r
    for a in awards:
        latest_award[a["procurement_id"]] = a
    bids_by_notice = defaultdict(list)
    bid_by_id = {}
    for b in bids:
        bids_by_notice[b["notice_id"]].append(b)
        bid_by_id[b["bid_id"]] = b

    # ---- KPIs and stages
    stages = {s: 0 for s in STATUSES}
    for p in procs.values():
        stages[p["status"]] = stages.get(p["status"], 0) + 1
    awarded_ids = [pid for pid in ids if pid in latest_award]
    awarded_value = sum(_f(latest_award[pid]["awarded_amount"]) for pid in awarded_ids)
    awarded_abc = sum(_f(procs[pid]["abc"]) for pid in awarded_ids)
    counted = [n for n in notices if n["bids_received"] is not None]
    kpis = {
        "tracked": len(procs),
        "totalAbc": round(sum(_f(p["abc"]) for p in procs.values()), 2),
        "awardedCount": len(awarded_ids),
        "awardedValue": round(awarded_value, 2),
        "savings": round(awarded_abc - awarded_value, 2),
        "savingsPct": round((awarded_abc - awarded_value) / awarded_abc * 100, 1) if awarded_abc else None,
        "avgBids": _avg([n["bids_received"] for n in counted]),
        "openNotices": stages.get("Posted (ITB/RFQ)", 0),
        "failed": stages.get("Failed Bidding", 0),
    }

    # ---- Timeliness: average days per stage + follow-up list
    t_post_deadline, t_deadline_res, t_res_award, t_award_contract, t_total = [], [], [], [], []
    follow_up, closing_soon = [], []
    for pid, p in procs.items():
        n, r, a = latest_notice.get(pid), latest_res.get(pid), latest_award.get(pid)
        if n:
            t_post_deadline.append(_days(n["published_date"], n["submission_deadline"]))
        if n and r:
            t_deadline_res.append(_days(n["submission_deadline"], r["resolution_date"]))
        if r and a:
            t_res_award.append(_days(r["resolution_date"], a["award_date"]))
        if a and a["contract_date"]:
            t_award_contract.append(_days(a["award_date"], a["contract_date"]))
        if n and a:
            t_total.append(_days(n["published_date"], a["award_date"]))

        item = {"id": p["reference_no"], "title": p["title"], "status": p["status"]}
        status = p["status"]
        if status == "Posted (ITB/RFQ)" and n and _d(n["submission_deadline"]):
            left = _days(today, n["submission_deadline"])
            if left is not None and 0 <= left <= alert_days:
                closing_soon.append({**item, "deadline": _d(n["submission_deadline"]).isoformat(), "daysLeft": left})
            late = -left if left is not None else None
            if late is not None and late > rules["followup_bids_days"]:
                follow_up.append({**item, "days": late, "issue": f"Deadline passed {late} days ago; bids not recorded"})
        elif status == "Under Evaluation" and n:
            late = _days(n["submission_deadline"], today)
            if late is not None and late > rules["followup_resolution_days"]:
                follow_up.append({**item, "days": late, "issue": f"{late} days since bid opening; no BAC resolution"})
        elif status == "Recommended for Award" and r:
            late = _days(r["resolution_date"], today)
            if late is not None and late > rules["followup_award_days"]:
                follow_up.append({**item, "days": late, "issue": f"{late} days since the resolution; no Notice of Award"})
        elif status == "Awarded" and a and a["contract_id"] is None:
            late = _days(a["award_date"], today)
            if late is not None and late > rules["followup_contract_days"]:
                follow_up.append({**item, "days": late, "issue": f"{late} days since the award; no contract recorded"})
    follow_up.sort(key=lambda x: -x["days"])
    closing_soon.sort(key=lambda x: x["daysLeft"])
    timeliness = {
        "avgDays": {"postingToDeadline": _avg(t_post_deadline), "deadlineToResolution": _avg(t_deadline_res),
                    "resolutionToAward": _avg(t_res_award), "awardToContract": _avg(t_award_contract),
                    "postingToAward": _avg(t_total)},
        "followUp": follow_up[:20], "followUpCount": len(follow_up),
        "closingSoon": closing_soon[:10], "alertDays": alert_days, "rules": rules,
    }

    # ---- Supplier concentration (awarded value per supplier)
    by_supplier = defaultdict(lambda: {"name": "", "awards": 0, "value": 0.0, "bids": 0, "wins": 0})
    for b in bids:
        s = by_supplier[b["supplier_id"]]
        s["name"] = b["supplier_name"]
        s["bids"] += 1
    for pid in awarded_ids:
        a = latest_award[pid]
        s = by_supplier[a["supplier_id"]]
        s["name"] = a["supplier_name"]
        s["awards"] += 1
        s["value"] += _f(a["awarded_amount"])
    top = sorted((v for v in by_supplier.values() if v["awards"]), key=lambda v: -v["value"])
    suppliers = [{"name": v["name"], "awards": v["awards"], "value": round(v["value"], 2), "bids": v["bids"],
                  "share": round(v["value"] / awarded_value * 100, 1) if awarded_value else 0.0} for v in top[:8]]
    top_share = suppliers[0]["share"] if suppliers else 0.0
    hhi = round(sum((v["value"] / awarded_value * 100) ** 2 for v in top), 0) if awarded_value else None

    # ---- Indicators for review
    flags = []
    for pid, p in procs.items():
        n = latest_notice.get(pid)
        if n and p["status"] in ("Under Evaluation", "Recommended for Award", "Awarded") and n["bids_received"] == 1:
            flags.append({"type": "single_bid", "id": p["reference_no"], "title": p["title"],
                          "detail": "Only one bid was received."})
        a = latest_award.get(pid)
        if a and a["bid_id"] in bid_by_id and bid_by_id[a["bid_id"]]["rank_no"] not in (None, 1):
            flags.append({"type": "not_lowest", "id": p["reference_no"], "title": p["title"],
                          "detail": f"Awarded to the rank-{bid_by_id[a['bid_id']]['rank_no']} bid "
                                    f"({a['supplier_name']}); the lower bids were found not responsive."})
    for v in suppliers:
        if len(awarded_ids) >= 3 and v["share"] >= HIGH_SHARE * 100:
            flags.append({"type": "high_share", "id": None, "title": v["name"],
                          "detail": f"{v['share']}% of the total awarded value ({v['awards']} award(s))."})
    # Bidders in the same procurement that share a representative (supplier_representatives -> persons)
    supplier_ids = sorted({b["supplier_id"] for b in bids})
    if supplier_ids:
        cur.execute("SELECT sr.supplier_id, sr.person_id, pe.full_name FROM supplier_representatives sr "
                    "JOIN persons pe ON pe.person_id = sr.person_id WHERE sr.supplier_id IN ("
                    + ", ".join(["%s"] * len(supplier_ids)) + ")", tuple(supplier_ids))
        reps = defaultdict(set)
        person_name = {}
        for r in cur.fetchall():
            reps[r["supplier_id"]].add(r["person_id"])
            person_name[r["person_id"]] = r["full_name"]
        for notice_id, nbids in bids_by_notice.items():
            seen = {}
            for b in nbids:
                for person in reps.get(b["supplier_id"], ()):
                    if person in seen and seen[person]["supplier_id"] != b["supplier_id"]:
                        p = procs[b["procurement_id"]]
                        flags.append({"type": "shared_rep", "id": p["reference_no"], "title": p["title"],
                                      "detail": f"{seen[person]['supplier_name']} and {b['supplier_name']} list the "
                                                f"same representative ({person_name[person]})."})
                    seen.setdefault(person, b)

    # ---- Health / completeness
    def pct(part, whole):
        return round(part / whole * 100, 1) if whole else None
    traced = sum(1 for pid in awarded_ids
                 if latest_award[pid]["bid_id"] is not None and latest_award[pid]["resolution_id"] is not None)
    with_contract = sum(1 for pid in awarded_ids if latest_award[pid]["contract_id"] is not None)
    posted_ids = [pid for pid in ids if pid in latest_notice]
    refs = rows("SELECT procurement_id, philgeps_ref FROM bid_notices WHERE procurement_id IN ({ids}) "
                "ORDER BY notice_id")
    last_ref = {r["procurement_id"]: r["philgeps_ref"] for r in refs}
    with_ref = sum(1 for pid in posted_ids if last_ref.get(pid))
    docs = _document_completeness(cur, awarded_ids)
    health = {
        "traceability": pct(traced, len(awarded_ids)),
        "contractsRecorded": pct(with_contract, len(awarded_ids)),
        "philgepsRefs": pct(with_ref, len(posted_ids)),
        "documents": docs,
    }

    # ---- Monthly awards
    monthly = defaultdict(lambda: {"count": 0, "value": 0.0})
    for pid in awarded_ids:
        d = _d(latest_award[pid]["award_date"])
        if d:
            key = f"{d.year}-{d.month:02d}"
            monthly[key]["count"] += 1
            monthly[key]["value"] += _f(latest_award[pid]["awarded_amount"])
    monthly_list = [{"month": k, "count": v["count"], "value": round(v["value"], 2)} for k, v in sorted(monthly.items())]

    cur.execute("SELECT actor_label, action, detail, tone, created_at FROM activity_log "
                "ORDER BY created_at DESC, log_id DESC LIMIT 8")
    recent = [{"actor": r["actor_label"] or "", "action": r["action"] or "", "detail": r["detail"] or "",
               "tone": r["tone"] or "info",
               "time": r["created_at"].strftime("%Y-%m-%d %H:%M") if hasattr(r["created_at"], "strftime")
               else str(r["created_at"] or "")[:16]} for r in cur.fetchall()]

    return {
        "fiscalYear": settings.get("active_fiscal_year"),
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "kpis": kpis, "stages": [{"name": s, "count": stages.get(s, 0)} for s in STATUSES], "timeliness": timeliness,
        "suppliers": suppliers, "concentration": {"topShare": top_share, "hhi": hhi,
                                                  "supplierCount": len(top)},
        "flags": flags, "health": health, "monthly": monthly_list, "recent": recent,
    }


def _document_completeness(cur, awarded_ids):
    """Share of awarded procurements with a Notice of Award and a contract document on file.
    None when the documents table is not installed yet (deploy/migrations/001_procurement_documents.sql)."""
    if not awarded_ids:
        return None
    try:
        cur.execute("SELECT procurement_id, doc_type FROM procurement_documents WHERE deleted_at IS NULL "
                    "AND procurement_id IN (" + ", ".join(["%s"] * len(awarded_ids)) + ")", tuple(awarded_ids))
        rows = cur.fetchall()
    except Exception:
        return None
    have = defaultdict(set)
    for r in rows:
        have[r["procurement_id"]].add(r["doc_type"])
    complete = sum(1 for pid in awarded_ids if {"Notice of Award", "Contract"} <= have[pid])
    return round(complete / len(awarded_ids) * 100, 1)


@dashboard_bp.route("/api/dashboard", methods=["GET"])
@require_role(*STAFF_ROLES)
def get_dashboard():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True, buffered=True)
        data = build_dashboard(cur)
        cur.close()
    except Exception:
        current_app.logger.exception("Could not build the dashboard")
        return jsonify({"status": "error", "message": "Could not load the dashboard figures."}), 500
    finally:
        if conn is not None:
            conn.close()
    response = jsonify({"status": "success", **data})
    response.headers["Cache-Control"] = "no-store"
    return response
