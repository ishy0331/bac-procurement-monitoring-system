"""
backend/registry.py - READ-ONLY procurement registry for proctest19.html.

Returns Santa Rosa procurement records (data_source 'sample' or 'encoded')
in exactly the object shape the frontend already uses in this.registry:

    id, title, unit, mode, category, abc, timeline, status, bidder, bidAmount,
    bids, philgeps, deadline, resolution, basis, noa, awardDate, noticeType

plus one extra field the current page ignores: dataSource.

Money is returned as JSON numbers, dates as "YYYY-MM-DD" strings.
This module only runs SELECT statements; it never changes data.
"""

from datetime import date, datetime
from decimal import Decimal

# One row per Santa Rosa procurement.
#   n  = the latest bid notice      (highest notice_id for the procurement)
#   r  = the latest BAC resolution  (highest resolution_id)
#   a  = the latest award notice    (highest award_id)
#   sb = the bid shown in the registry ("shown bid"):
#          1. the awarded bid, else
#          2. the bid recommended by the latest resolution, else
#          3. the lowest-ranked bid recorded under the latest notice
#   s  = the supplier of the shown bid
REGISTRY_SQL = """
SELECT
    p.procurement_id,
    p.reference_no,
    p.title,
    p.end_user_unit,
    p.procurement_class,
    p.abc,
    p.target_timeline,
    p.status,
    p.data_source,
    m.name                AS mode_name,
    n.notice_type,
    n.philgeps_ref,
    n.submission_deadline,
    n.bids_received,
    r.resolution_no,
    r.recommendation,
    a.notice_no           AS award_notice_no,
    a.award_date,
    sb.bid_amount         AS shown_bid_amount,
    s.name                AS shown_supplier_name
FROM procurements p
JOIN procurement_modes m
       ON m.mode_id = p.mode_id
LEFT JOIN bid_notices n
       ON n.notice_id = (SELECT MAX(n2.notice_id)
                           FROM bid_notices n2
                          WHERE n2.procurement_id = p.procurement_id)
LEFT JOIN bac_resolutions r
       ON r.resolution_id = (SELECT MAX(r2.resolution_id)
                               FROM bac_resolutions r2
                              WHERE r2.procurement_id = p.procurement_id)
LEFT JOIN award_notices a
       ON a.award_id = (SELECT MAX(a2.award_id)
                          FROM award_notices a2
                         WHERE a2.procurement_id = p.procurement_id)
LEFT JOIN bids sb
       ON sb.bid_id = COALESCE(
              a.bid_id,
              r.recommended_bid_id,
              (SELECT b3.bid_id
                 FROM bids b3
                WHERE b3.notice_id = n.notice_id
                ORDER BY b3.rank_no IS NULL, b3.rank_no, b3.bid_amount, b3.bid_id
                LIMIT 1))
LEFT JOIN suppliers s
       ON s.supplier_id = sb.supplier_id
WHERE p.data_source IN ('sample', 'encoded')
ORDER BY p.procurement_id
"""

# Same labels the frontend already displays (see logResolution in proctest19.html).
BASIS_LABELS = {
    "LCRB": "Lowest Calculated Responsive Bid (LCRB)",
    "SCRB": "Single Calculated Responsive Bid (SCRB)",
    "FAIL": "Failure of Bidding",
}


def _number(value):
    """DECIMAL -> JSON number (the database itself keeps the exact value)."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return value


def _iso_date(value):
    """DATE/DATETIME -> 'YYYY-MM-DD' (the format parseD() in the page expects)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def row_to_frontend(row):
    """Map one SQL row (a dict) to the frontend's procurement object."""
    return {
        "id": row["reference_no"],
        "title": row["title"],
        "unit": row["end_user_unit"],
        "mode": row["mode_name"],
        "category": row["procurement_class"],
        "abc": _number(row["abc"]),
        "timeline": row["target_timeline"],
        "status": row["status"],
        "bidder": row["shown_supplier_name"] or "",
        "bidAmount": _number(row["shown_bid_amount"]),
        "bids": row["bids_received"],
        "philgeps": row["philgeps_ref"],
        "deadline": _iso_date(row["submission_deadline"]),
        "resolution": row["resolution_no"],
        "basis": BASIS_LABELS.get(row["recommendation"]),
        "noa": row["award_notice_no"],
        "awardDate": _iso_date(row["award_date"]),
        "noticeType": row["notice_type"],
        "dataSource": row["data_source"],
    }


def fetch_registry(conn):
    """Run the read-only registry query on an open connection."""
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(REGISTRY_SQL)
        rows = cur.fetchall()
    finally:
        cur.close()
    return [row_to_frontend(row) for row in rows]