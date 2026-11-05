"""
backend/public_registry.py - PUBLIC view of the procurement registry.

Used by GET /api/public/procurements (no login).

It runs its OWN read-only query (PUBLIC_REGISTRY_SQL) that selects only the
columns that may be shown to the public. Internal columns - bid counts, losing
or evaluated bids, BAC resolutions, recommendation basis, target timeline, data
source - are never selected, so they cannot leak through this endpoint.
It does not use registry.py's "shown bid", which may be an awarded,
recommended or merely lowest-ranked bid.

Then an allowlist decides, per status, which fields are returned:

  Posted (ITB/RFQ)  -> the published notice: reference number, title,
                       end-user unit, mode, category, ABC, notice type,
                       PhilGEPS reference, submission deadline.
                       Only if a bid notice is actually recorded.
  Awarded           -> the above plus the recorded award: awardee (the
                       supplier named on the award notice), contract amount
                       (award_notices.awarded_amount), Notice of Award number
                       and award date. Only if a Notice of Award is recorded.
  Any other status  -> not returned (Pre-Procurement, Under Evaluation,
                       Recommended for Award, Failed Bidding).

Field names: "id" is the procurement REFERENCE NUMBER (procurements.reference_no,
e.g. SR-2026-001), not the database ID. The public page shows it in the
Reference column and uses it to match rows.

Contract amount: the schema has no amount column in `contracts` (its comment:
"amount = SUM of its award_notices; not stored twice"). The official recorded
figure is therefore award_notices.awarded_amount - the amount stated on the
Notice of Award - and that is what "contractAmount" returns.

One award notice per procurement is NOT guaranteed by the schema:
uq_awards_proc_lot (procurement_id, lot_no) allows several rows with
lot_no NULL, because MariaDB treats NULLs in a UNIQUE key as distinct.
Santa Rosa awards whole procurements (one Notice of Award, lot_no NULL), so a
second award notice would mean a re-award, a correction, lots, or a data error
- and the right amount differs (latest notice vs. sum). Rather than guess, an
award is published only when the procurement has EXACTLY ONE award notice;
otherwise the record is withheld until the BAC decides the rule.

PROVISIONAL POLICY - not legally confirmed. Verify the permitted public
information with the BAC source before real records are used.
"""

from datetime import date, datetime
from decimal import Decimal

POSTED = "Posted (ITB/RFQ)"
AWARDED = "Awarded"

# Only public columns are selected.
#   n = the latest bid notice  (highest notice_id for the procurement)
#   a = the latest award notice (highest award_id; Santa Rosa awards whole
#       procurements, lot_no NULL - lot-level bidding is out of scope)
#   s = the supplier named on that award notice
PUBLIC_REGISTRY_SQL = """
SELECT
    p.reference_no,
    p.title,
    p.end_user_unit,
    p.procurement_class,
    p.abc,
    p.status,
    m.name             AS mode_name,
    n.notice_id,
    n.notice_type,
    n.philgeps_ref,
    n.submission_deadline,
    a.award_id,
    a.notice_no        AS award_notice_no,
    a.award_date,
    a.awarded_amount,
    s.name             AS awardee_name,
    (SELECT COUNT(*)
       FROM award_notices a3
      WHERE a3.procurement_id = p.procurement_id) AS award_count
FROM procurements p
JOIN procurement_modes m
       ON m.mode_id = p.mode_id
LEFT JOIN bid_notices n
       ON n.notice_id = (SELECT MAX(n2.notice_id)
                           FROM bid_notices n2
                          WHERE n2.procurement_id = p.procurement_id)
LEFT JOIN award_notices a
       ON a.award_id = (SELECT MAX(a2.award_id)
                          FROM award_notices a2
                         WHERE a2.procurement_id = p.procurement_id)
LEFT JOIN suppliers s
       ON s.supplier_id = a.supplier_id
WHERE p.data_source IN ('sample', 'encoded')
  AND p.status IN ('Posted (ITB/RFQ)', 'Awarded')
  AND p.archived_at IS NULL           -- archived (soft-deleted) records are never public
ORDER BY p.procurement_id
"""

NOTICE_FIELDS = ("id", "title", "unit", "mode", "category", "abc", "status",
                 "noticeType", "philgeps", "deadline")
AWARD_FIELDS = NOTICE_FIELDS + ("awardee", "contractAmount", "noa", "awardDate")

PUBLIC_FIELDS_BY_STATUS = {
    POSTED: NOTICE_FIELDS,
    AWARDED: AWARD_FIELDS,
}


def _number(value):
    """DECIMAL -> JSON number."""
    if value is None:
        return None
    return float(value) if isinstance(value, Decimal) else value


def _iso_date(value):
    """DATE/DATETIME -> 'YYYY-MM-DD'."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def row_to_public(row):
    """Map one SQL row to its public record, or None if it is not public."""
    status = row["status"]
    fields = PUBLIC_FIELDS_BY_STATUS.get(status)
    if fields is None:
        return None
    if row["notice_id"] is None:
        return None          # no bid notice recorded: nothing published yet
    if status == AWARDED and (row["award_id"] is None or not row["award_notice_no"]):
        return None          # an award is public only once its Notice of Award is recorded
    if status == AWARDED and row["award_count"] != 1:
        return None          # several award notices: amount ambiguous (latest vs. sum) - withheld

    full = {
        "id": row["reference_no"],
        "title": row["title"],
        "unit": row["end_user_unit"],
        "mode": row["mode_name"],
        "category": row["procurement_class"],
        "abc": _number(row["abc"]),
        "status": status,
        "noticeType": row["notice_type"],
        "philgeps": row["philgeps_ref"],
        "deadline": _iso_date(row["submission_deadline"]),
        "awardee": row["awardee_name"],
        "contractAmount": _number(row["awarded_amount"]),
        "noa": row["award_notice_no"],
        "awardDate": _iso_date(row["award_date"]),
    }
    return {field: full[field] for field in fields}


def fetch_public_registry(conn):
    """Run the public query and keep only published records and allowed fields."""
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(PUBLIC_REGISTRY_SQL)
        rows = cur.fetchall()
    finally:
        cur.close()
    public = []
    for row in rows:
        item = row_to_public(row)
        if item is not None:
            public.append(item)
    return public
