"""
backend/documents_api.py - Document management (bid bulletins, resolutions, notices of award, contracts ...).

Staff (signed in)
    GET  /api/documents?q=&type=                   search every document (reference no., title, file name, type)
    GET  /api/procurements/<ref>/documents         documents of one procurement
    POST /api/procurements/<ref>/documents         upload (Secretariat): multipart file + docType + title + isPublic
    GET  /api/documents/<id>/download              download (staff)
    POST /api/documents/<id>/visibility            {isPublic: true|false} (Secretariat)
    POST /api/documents/<id>/remove                soft-remove with a reason (Secretariat); the file is kept
Public (no sign-in)
    GET  /api/public/documents?q=                  documents marked public, of City records that are not archived
    GET  /api/public/documents/<id>/download

Storage
  * Metadata in the procurement_documents table (created once by backend/migrate_documents.py, run as root).
  * Files in UPLOAD_DIR (config.py; default <project>/uploads), OUTSIDE every served folder, under a random
    name. They are only sent through the download endpoints above, never by their real path.
  * Allowed: PDF, PNG, JPG, DOCX, XLSX up to MAX_UPLOAD_MB (default 20). The first bytes must match the type.
  * A SHA-256 of each file is stored, so a later copy can be checked against the uploaded original.
  * Every upload, visibility change and removal is written to activity_log. Nothing is physically deleted.
"""

import hashlib
import os
import re
import uuid
from pathlib import Path

from flask import Blueprint, current_app, g, jsonify, request, send_file

import config
from auth import require_role
from procurement_api import (ApiError, ValidationError, REFERENCE_PATTERN, clean_text, json_error,
                             lock_procurement, log_activity, run_write)
from db import get_db_connection

documents_bp = Blueprint("documents", __name__)

STAFF_ROLES = ("secretariat", "member", "admin")
BACKEND_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = Path(getattr(config, "UPLOAD_DIR", BACKEND_DIR.parent / "uploads"))
MAX_UPLOAD_MB = int(getattr(config, "MAX_UPLOAD_MB", 20))

DOC_TYPES = ("Purchase Request", "Invitation to Bid / RFQ", "Bid Bulletin", "Abstract of Bids",
             "Post-Qualification Report", "BAC Resolution", "Notice of Award", "Contract", "Notice to Proceed",
             "Other")
# extension -> (mime type, check of the first bytes)
FILE_TYPES = {
    "pdf": ("application/pdf", lambda b: b.startswith(b"%PDF")),
    "png": ("image/png", lambda b: b.startswith(b"\x89PNG\r\n\x1a\n")),
    "jpg": ("image/jpeg", lambda b: b.startswith(b"\xff\xd8\xff")),
    "jpeg": ("image/jpeg", lambda b: b.startswith(b"\xff\xd8\xff")),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", lambda b: b.startswith(b"PK\x03\x04")),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", lambda b: b.startswith(b"PK\x03\x04")),
}
NOT_INSTALLED = ("Document management is not installed yet. Ask the administrator to run "
                 "python backend\\migrate_documents.py once (as the MariaDB root user).")

LIST_SQL = """
SELECT d.document_id, d.doc_type, d.title, d.original_name, d.mime_type, d.size_bytes, d.sha256, d.is_public,
       d.uploaded_at, u.full_name AS uploaded_by_name, p.reference_no, p.title AS procurement_title, p.status
FROM procurement_documents d
JOIN procurements p ON p.procurement_id = d.procurement_id
LEFT JOIN users u   ON u.user_id = d.uploaded_by
WHERE d.deleted_at IS NULL AND p.data_source IN ('sample', 'encoded') AND p.archived_at IS NULL
"""


class NotInstalled(Exception):
    pass


def _is_missing_table(exc):
    return getattr(exc, "errno", None) == 1146 or "no such table" in str(exc).lower()


def _doc_json(r, public=False):
    out = {"documentId": r["document_id"], "docType": r["doc_type"], "title": r["title"],
           "fileName": r["original_name"], "sizeBytes": int(r["size_bytes"] or 0),
           "uploadedAt": r["uploaded_at"].strftime("%Y-%m-%d %H:%M") if hasattr(r["uploaded_at"], "strftime")
           else str(r["uploaded_at"] or "")[:16],
           "reference": r["reference_no"], "procurementTitle": r["procurement_title"]}
    if not public:
        out.update(isPublic=bool(r["is_public"]), uploadedBy=r["uploaded_by_name"] or "", sha256=r["sha256"],
                   status=r["status"])
    return out


def _search_filters(public):
    sql, params = LIST_SQL, []
    if public:
        sql += " AND d.is_public = 1"
    q = (request.args.get("q") or "").strip()[:100]
    if q:
        like = f"%{q}%"
        sql += (" AND (p.reference_no LIKE %s OR p.title LIKE %s OR d.title LIKE %s OR d.original_name LIKE %s"
                " OR d.doc_type LIKE %s)")
        params += [like] * 5
    doc_type = request.args.get("type")
    if doc_type in DOC_TYPES:
        sql += " AND d.doc_type = %s"
        params.append(doc_type)
    return sql + " ORDER BY d.uploaded_at DESC, d.document_id DESC LIMIT 500", params


def _read(sql, params):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True, buffered=True)
        try:
            cur.execute(sql, tuple(params))
        except Exception as exc:
            if _is_missing_table(exc):
                raise NotInstalled() from exc
            raise
        rows = cur.fetchall()
        cur.close()
        return rows
    finally:
        if conn is not None:
            conn.close()


def _list_reply(sql, params, public):
    try:
        rows = _read(sql, params)
    except NotInstalled:
        return json_error(NOT_INSTALLED, 503, code="documents_not_installed")
    except Exception:
        current_app.logger.exception("Could not list documents")
        return json_error("Could not load the documents.", 500)
    response = jsonify({"status": "success", "docTypes": list(DOC_TYPES), "maxUploadMb": MAX_UPLOAD_MB,
                        "documents": [_doc_json(r, public) for r in rows]})
    response.headers["Cache-Control"] = "no-store"
    return response


@documents_bp.route("/api/documents", methods=["GET"])
@require_role(*STAFF_ROLES)
def search_documents():
    sql, params = _search_filters(public=False)
    return _list_reply(sql, params, public=False)


@documents_bp.route("/api/public/documents", methods=["GET"])
def public_documents():
    sql, params = _search_filters(public=True)
    return _list_reply(sql, params, public=True)


@documents_bp.route("/api/procurements/<reference>/documents", methods=["GET"])
@require_role(*STAFF_ROLES)
def procurement_documents(reference):
    if not REFERENCE_PATTERN.match(reference or ""):
        return json_error("Procurement not found.", 404)
    return _list_reply(LIST_SQL + " AND p.reference_no = %s ORDER BY d.uploaded_at DESC, d.document_id DESC",
                       [reference], public=False)


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------
def _check_file(upload):
    """Returns (extension, mime, bytes) or raises ValidationError."""
    if upload is None or not upload.filename:
        raise ValidationError({"file": "Choose a file to upload."})
    name = os.path.basename(upload.filename.replace("\\", "/"))
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in FILE_TYPES:
        raise ValidationError({"file": "Only PDF, PNG, JPG, DOCX and XLSX files can be uploaded."})
    data = upload.read(MAX_UPLOAD_MB * 1024 * 1024 + 1)
    if not data:
        raise ValidationError({"file": "The file is empty."})
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise ValidationError({"file": f"The file is larger than {MAX_UPLOAD_MB} MB."})
    mime, check = FILE_TYPES[ext]
    if not check(data[:16]):
        raise ValidationError({"file": f"The file content does not look like a .{ext} file."})
    return name[:255], ext, mime, data


@documents_bp.route("/api/procurements/<reference>/documents", methods=["POST"])
@require_role("secretariat")
def upload_document(reference):
    if not REFERENCE_PATTERN.match(reference or ""):
        return json_error("Procurement not found.", 404)
    try:
        form = request.form
        errors = {}
        doc_type = form.get("docType")
        if doc_type not in DOC_TYPES:
            errors["docType"] = "Select the document type."
        title = clean_text(form, "title", errors, label="Document title", max_len=255, required=False)
        is_public = form.get("isPublic") in ("1", "true", "on")
        if errors:
            raise ValidationError(errors)
        original, ext, mime, data = _check_file(request.files.get("file"))
    except ValidationError as exc:
        return json_error("Please correct the highlighted fields. Nothing was saved.", 400, errors=exc.errors)

    digest = hashlib.sha256(data).hexdigest()
    stored = f"{uuid.uuid4().hex}.{ext}"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOAD_DIR / stored
    path.write_bytes(data)                       # written first; removed again if the database save fails

    def tx(cur):
        locked = lock_procurement(cur, reference)
        try:
            cur.execute("INSERT INTO procurement_documents (procurement_id, doc_type, title, original_name, stored_name, "
                        "mime_type, size_bytes, sha256, is_public, uploaded_by) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (locked["procurement_id"], doc_type, title or original, original, stored, mime, len(data),
                         digest, 1 if is_public else 0, g.user["user_id"]))
        except Exception as exc:
            if _is_missing_table(exc):
                raise NotInstalled() from exc
            raise
        doc_id = cur.lastrowid
        log_activity(cur, "Document uploaded",
                     f"{locked['reference_no']}: {doc_type} '{title or original}' ({original}, {len(data):,} bytes, "
                     f"SHA-256 {digest[:12]}...){' - public' if is_public else ''}.",
                     entity_type="document", entity_id=doc_id, tone="ok")
        return {"documentId": doc_id, "reference": locked["reference_no"]}

    try:
        result = run_write(tx)
    except Exception as exc:
        try:
            path.unlink()
        except OSError:
            current_app.logger.warning("Could not remove the unsaved upload %s", stored)
        if isinstance(exc, NotInstalled):
            return json_error(NOT_INSTALLED, 503, code="documents_not_installed")
        if isinstance(exc, ApiError):
            return json_error(exc.message, exc.status, **exc.extra)
        current_app.logger.exception("Upload for %s failed", reference)
        return json_error("Could not save the document. Nothing was saved.", 500)
    return jsonify({"status": "success", "message": f"{doc_type} uploaded to {result['reference']}.",
                    "documentId": result["documentId"]}), 201


# ---------------------------------------------------------------------------
# Download / visibility / remove
# ---------------------------------------------------------------------------
def _send(doc_id, public):
    sql = ("SELECT d.document_id, d.original_name, d.stored_name, d.mime_type, d.is_public FROM procurement_documents d "
           "JOIN procurements p ON p.procurement_id = d.procurement_id WHERE d.document_id = %s AND d.deleted_at IS NULL "
           "AND p.data_source IN ('sample', 'encoded') AND p.archived_at IS NULL")
    try:
        rows = _read(sql + (" AND d.is_public = 1" if public else ""), [doc_id])
    except NotInstalled:
        return json_error(NOT_INSTALLED, 503, code="documents_not_installed")
    except Exception:
        current_app.logger.exception("Could not read document %s", doc_id)
        return json_error("Could not load the document.", 500)
    if not rows:
        return json_error("Document not found.", 404)
    row = rows[0]
    if not re.fullmatch(r"[0-9a-f]{32}\.[a-z]{3,4}", row["stored_name"] or ""):
        return json_error("Document not found.", 404)
    path = UPLOAD_DIR / row["stored_name"]
    if not path.is_file():
        current_app.logger.error("Document file missing: %s", row["stored_name"])
        return json_error("The file of this document is missing on the server.", 410)
    inline = row["mime_type"] in ("application/pdf", "image/png", "image/jpeg") and request.args.get("inline") == "1"
    response = send_file(path, mimetype=row["mime_type"], as_attachment=not inline,
                         download_name=row["original_name"], max_age=0)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, no-store" if not public else "no-cache"
    return response


@documents_bp.route("/api/documents/<int:doc_id>/download", methods=["GET"])
@require_role(*STAFF_ROLES)
def download_document(doc_id):
    return _send(doc_id, public=False)


@documents_bp.route("/api/public/documents/<int:doc_id>/download", methods=["GET"])
def public_download(doc_id):
    return _send(doc_id, public=True)


def _lock_document(cur, doc_id):
    try:
        cur.execute("SELECT d.document_id, d.procurement_id, d.doc_type, d.title, d.is_public, d.deleted_at, "
                    "p.reference_no FROM procurement_documents d JOIN procurements p ON p.procurement_id = d.procurement_id "
                    "WHERE d.document_id = %s FOR UPDATE", (doc_id,))
    except Exception as exc:
        if _is_missing_table(exc):
            raise NotInstalled() from exc
        raise
    row = cur.fetchone()
    if row is None or row["deleted_at"] is not None:
        raise ApiError("Document not found.", 404)
    return row


def _doc_write(tx, failure):
    try:
        result = run_write(tx)
    except NotInstalled:
        return json_error(NOT_INSTALLED, 503, code="documents_not_installed")
    except ValidationError as exc:
        return json_error("Please correct the highlighted fields.", 400, errors=exc.errors)
    except ApiError as exc:
        return json_error(exc.message, exc.status, **exc.extra)
    except Exception:
        current_app.logger.exception(failure)
        return json_error(failure, 500)
    return jsonify({"status": "success", **result})


@documents_bp.route("/api/documents/<int:doc_id>/visibility", methods=["POST"])
@require_role("secretariat")
def set_visibility(doc_id):
    payload = request.get_json(silent=True) or {}
    make_public = payload.get("isPublic") is True

    def tx(cur):
        row = _lock_document(cur, doc_id)
        if bool(row["is_public"]) == make_public:
            return {"message": "No change."}
        cur.execute("UPDATE procurement_documents SET is_public = %s WHERE document_id = %s",
                    (1 if make_public else 0, doc_id))
        log_activity(cur, "Document visibility changed",
                     f"{row['reference_no']}: {row['doc_type']} '{row['title']}' is now "
                     f"{'shown on the Public Portal' if make_public else 'internal only'}.",
                     entity_type="document", entity_id=doc_id)
        return {"message": "Shown on the Public Portal." if make_public else "Now internal only."}
    return _doc_write(tx, "Could not change the visibility.")


@documents_bp.route("/api/documents/<int:doc_id>/remove", methods=["POST"])
@require_role("secretariat")
def remove_document(doc_id):
    payload = request.get_json(silent=True) or {}

    def tx(cur):
        errors = {}
        reason = clean_text(payload, "reason", errors, label="Reason", max_len=500)
        if reason and len(reason) < 5:
            errors["reason"] = "Reason must be at least 5 characters."
        if errors:
            raise ValidationError(errors)
        row = _lock_document(cur, doc_id)
        cur.execute("UPDATE procurement_documents SET deleted_at = NOW(), deleted_by = %s, is_public = 0 "
                    "WHERE document_id = %s", (g.user["user_id"], doc_id))
        log_activity(cur, "Document removed",
                     f"{row['reference_no']}: {row['doc_type']} '{row['title']}' removed from the list "
                     f"(the file is kept on the server). Reason: {reason}",
                     entity_type="document", entity_id=doc_id, tone="warn")
        return {"message": "Document removed from the list. The file is kept on the server."}
    return _doc_write(tx, "Could not remove the document.")
