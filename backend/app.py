import sys
from pathlib import Path

from flask import Flask, jsonify, send_from_directory

import config                                      # backend/config.py (not in Git)
from db import get_db_connection
from registry import fetch_registry
from public_registry import fetch_public_registry   # published records only
from auth import init_auth, require_role           # Phase 2: authentication
from procurement_api import procurement_bp         # Phase 3: procurement records
from graph_api import graph_bp                     # City Knowledge Graph (sample + encoded records)
from workflow_api import workflow_bp               # notice -> bids -> resolution -> award -> contract
from dashboard_api import dashboard_bp             # dashboard figures computed from the database
from admin_api import admin_bp                     # Super Admin: accounts and system settings
from documents_api import documents_bp, MAX_UPLOAD_MB   # uploaded procurement documents
from reports_api import reports_bp                 # PMR as Excel (.xlsx)
import graph_autosync                              # keeps Neo4j in step with MariaDB after every save

STAFF_ROLES = ("secretariat", "member", "admin")

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent
FRONTEND_FILE = "proctest19.html"
VENDOR_DIR = PROJECT_DIR / "vendor"

# All database work must stay on the isolated frontend test database for now.
EXPECTED_DATABASE = "procurement_db_frontend_test"


def _load_secret_key():
    """SECRET_KEY signs the session cookie. Refuse to start without a strong one."""
    key = getattr(config, "SECRET_KEY", None)
    if not isinstance(key, str) or len(key) < 32:
        raise RuntimeError(
            "backend/config.py must define SECRET_KEY (at least 32 characters). "
            "Generate one with:  python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    return key


app = Flask(__name__, static_folder=None)
app.config.update(
    SECRET_KEY=_load_secret_key(),
    SESSION_COOKIE_NAME="procurement_session",
    SESSION_COOKIE_HTTPONLY=True,          # page scripts cannot read the cookie
    SESSION_COOKIE_SAMESITE="Lax",         # not sent with cross-site requests
    # True only when the site runs on HTTPS (cannot work on http://127.0.0.1).
    SESSION_COOKIE_SECURE=bool(getattr(config, "SESSION_COOKIE_SECURE", False)),
    MAX_CONTENT_LENGTH=(MAX_UPLOAD_MB + 1) * 1024 * 1024,   # largest accepted request (document uploads)
)
init_auth(app)   # /api/login, /api/change-password, /api/logout, /api/me + session checks
app.register_blueprint(procurement_bp)   # Phase 3: /api/procurements/<reference> (read-only in Step 1)
app.register_blueprint(graph_bp)         # GET /api/graph/sample (staff only, SELECT only)
app.register_blueprint(workflow_bp)      # POST /api/procurements/<ref>/notice | bids | resolution | award | contract
app.register_blueprint(dashboard_bp)     # GET /api/dashboard
app.register_blueprint(admin_bp)         # /api/admin/users, /api/admin/settings
app.register_blueprint(documents_bp)     # /api/documents..., /api/public/documents...
app.register_blueprint(reports_bp)       # /api/reports/pmr.xlsx, /api/public/reports/pmr.xlsx
graph_autosync.start()                   # background MariaDB -> Neo4j sync + one catch-up sync at start-up


# Serve the existing staff frontend
@app.route("/")
def index():
    return send_from_directory(PROJECT_DIR, FRONTEND_FILE)


# Local copies of Tailwind and Font Awesome (vendor/), so the pages work
# without an internet connection. Download them with deploy/get_vendor_assets.ps1.
@app.route("/vendor/<path:filename>")
def vendor_file(filename):
    return send_from_directory(VENDOR_DIR, filename)



# API root
@app.route("/api")
def api_root():
    return jsonify({
        "message": "Procurement Monitoring System Backend is running!"
    })


# Check if Flask is working
@app.route("/api/health")
def health():
    return jsonify({
        "status": "success",
        "message": "Flask API is working!"
    })


# Check database connection and available tables (Super Admin only)
@app.route("/api/db-test")
@require_role("admin")
def db_test():
    connection = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor()

        cursor.execute("SELECT DATABASE()")
        database = cursor.fetchone()[0]

        cursor.execute("SHOW TABLES")
        tables = [row[0] for row in cursor.fetchall()]

        cursor.close()

        return jsonify({
            "status": "success",
            "database": database,
            "table_count": len(tables),
            "tables": tables
        })

    except Exception:
        # Full error details go to the Flask terminal only.
        app.logger.exception("Database test failed")

        return jsonify({
            "status": "error",
            "message": "Database test failed. See the Flask terminal for details."
        }), 500

    finally:
        if connection is not None:
            connection.close()


# INTERNAL procurement registry (READ-ONLY): signed-in BAC staff only.
# Includes records still under deliberation, bid details and BAC resolutions.
# Anonymous visitors get 401; restricted (password-change) sessions get 403.
@app.route("/api/procurements")
@require_role(*STAFF_ROLES)
def get_procurements():
    connection = None

    try:
        connection = get_db_connection()
        procurements = fetch_registry(connection)

        return jsonify({
            "status": "success",
            "count": len(procurements),
            "procurements": procurements
        })

    except Exception:
        # Full error details go to the Flask terminal only.
        app.logger.exception(
            "Failed to load the procurement registry"
        )

        return jsonify({
            "status": "error",
            "message": "Could not load procurement records."
        }), 500

    finally:
        if connection is not None:
            connection.close()

# INTERNAL activity log (READ-ONLY): signed-in BAC staff only.
@app.route("/api/activity-log")
@require_role(*STAFF_ROLES)
def get_activity_log():
    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT actor_label, action, detail, created_at
            FROM activity_log
            ORDER BY created_at DESC, log_id DESC
        """)

        records = []
        for row in cursor.fetchall():
            records.append({
                "timestamp": (
                    row["created_at"].strftime("%Y-%m-%d %H:%M:%S")
                    if row["created_at"] else ""
                ),
                "actor": row["actor_label"] or "",
                "event": row["action"] or "",
                "detail": row["detail"] or ""
            })

        return jsonify({
            "status": "success",
            "count": len(records),
            "records": records
        })

    except Exception:
        app.logger.exception("Failed to load the database activity log")
        return jsonify({
            "status": "error",
            "message": "Could not load the saved activity log."
        }), 500

    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()

# PUBLIC procurement registry (READ-ONLY, no login): only published records
# and only the fields approved for public viewing (see public_registry.py).
@app.route("/api/public/procurements")
def get_public_procurements():
    connection = None

    try:
        connection = get_db_connection()
        procurements = fetch_public_registry(connection)

        return jsonify({
            "status": "success",
            "count": len(procurements),
            "procurements": procurements
        })

    except Exception:
        app.logger.exception("Failed to load the public procurement registry")

        return jsonify({
            "status": "error",
            "message": "Could not load public procurement records."
        }), 500

    finally:
        if connection is not None:
            connection.close()


def verify_database():
    """Refuse to start unless DB_CONFIG points at the test database."""
    connection = get_db_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT DATABASE()")
        database = cursor.fetchone()[0]
        cursor.close()
    finally:
        connection.close()

    if database != EXPECTED_DATABASE:
        sys.exit(
            f"Refusing to start: connected to '{database}', expected "
            f"'{EXPECTED_DATABASE}'. Check DB_CONFIG in backend/config.py."
        )
    print(f"Database check passed: using '{database}'.")


if __name__ == "__main__":
    # Development server only. For deployment use:  python backend\serve.py
    verify_database()
    app.run(host="127.0.0.1", port=5000,
            debug=bool(getattr(config, "DEBUG", False)))
