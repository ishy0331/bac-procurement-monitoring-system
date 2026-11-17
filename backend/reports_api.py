"""
backend/reports_api.py - Procurement Monitoring Report (PMR) as an Excel workbook (READ-ONLY).

    GET /api/reports/pmr.xlsx            staff: the internal registry (same columns as the staff PMR CSV)
    GET /api/public/reports/pmr.xlsx     anyone: published records, public fields only (same as the public CSV)

The PDF version is produced in the browser (print-ready page -> "Save as PDF"), from the same records.
Built with xlsx_writer.py (standard library only, no extra package to install).
"""

from datetime import date

from flask import Blueprint, Response, current_app, jsonify

from auth import require_role
from db import get_db_connection
from public_registry import fetch_public_registry
from registry import fetch_registry
from xlsx_writer import build_xlsx

reports_bp = Blueprint("reports", __name__)

STAFF_ROLES = ("secretariat", "member", "admin")
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

STAFF_HEADER = ["Procurement ID", "Project Title", "End-User Unit", "Mode of Procurement", "Category", "ABC (PHP)",
                "PhilGEPS Reference", "Bid Submission Deadline", "No. of Bids", "Bidder / Awardee",
                "Bid / Contract Amount (PHP)", "BAC Resolution", "Notice of Award", "Award Date", "Contract No.", "Status"]
PUBLIC_HEADER = ["Reference No.", "Project Title", "End-User Unit", "Mode of Procurement", "Category", "ABC (PHP)",
                 "Notice Type", "PhilGEPS Reference", "Bid Submission Deadline", "Status", "Awardee",
                 "Contract Amount (PHP)", "Notice of Award", "Award Date"]


def _xlsx(name, title, header, rows, money_cols, widths):
    data = build_xlsx("PMR", [header] + rows, title=title, money_cols=money_cols, col_widths=widths)
    return Response(data, mimetype=XLSX_MIME, headers={
        "Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


def _load(fetch):
    conn = get_db_connection()
    try:
        return fetch(conn)
    finally:
        conn.close()


@reports_bp.route("/api/reports/pmr.xlsx", methods=["GET"])
@require_role(*STAFF_ROLES)
def staff_pmr_xlsx():
    try:
        records = _load(fetch_registry)
    except Exception:
        current_app.logger.exception("PMR export failed")
        return jsonify({"status": "error", "message": "Could not build the PMR."}), 500
    rows = [[r["id"], r["title"], r["unit"], r["mode"], r["category"], r["abc"], r["philgeps"] or "",
             r["deadline"] or "", r["bids"] if r["bids"] is not None else "", r["bidder"] or "",
             r["bidAmount"] if r["bidAmount"] is not None else "", r["resolution"] or "", r["noa"] or "",
             r["awardDate"] or "", r.get("contractNo") or "", r["status"]] for r in records]
    title = f"Procurement Monitoring Report - generated {date.today().isoformat()} ({len(rows)} projects, internal)"
    return _xlsx(f"PMR_SantaRosa_{date.today().isoformat()}.xlsx", title, STAFF_HEADER, rows, (5, 10),
                 [16, 48, 26, 30, 16, 16, 18, 16, 10, 32, 18, 22, 22, 13, 18, 22])


@reports_bp.route("/api/public/reports/pmr.xlsx", methods=["GET"])
def public_pmr_xlsx():
    try:
        records = _load(fetch_public_registry)
    except Exception:
        current_app.logger.exception("Public PMR export failed")
        return jsonify({"status": "error", "message": "Could not build the PMR."}), 500
    rows = [[r["id"], r["title"], r["unit"], r["mode"], r["category"], r["abc"], r.get("noticeType") or "",
             r.get("philgeps") or "", r.get("deadline") or "", r["status"], r.get("awardee") or "",
             r["contractAmount"] if r.get("contractAmount") is not None else "", r.get("noa") or "",
             r.get("awardDate") or ""] for r in records]
    title = f"Procurement Monitoring Report (public extract) - generated {date.today().isoformat()}"
    return _xlsx(f"PMR_SantaRosa_Public_{date.today().isoformat()}.xlsx", title, PUBLIC_HEADER, rows, (5, 11),
                 [16, 48, 26, 30, 16, 16, 10, 18, 16, 22, 32, 18, 22, 13])
