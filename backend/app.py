import sys
from pathlib import Path

from flask import Flask, jsonify, send_from_directory

import config                                      # backend/config.py (not in Git)
from db import get_db_connection
from registry import fetch_registry
from public_registry import fetch_public_registry   # published records only
from auth import init_auth, require_role           # Phase 2: authentication

STAFF_ROLES = ("secretariat", "member", "admin")

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent
FRONTEND_FILE = "proctest19.html"

# All database work must stay on the test database for now.
EXPECTED_DATABASE = "procurement_db_test"


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
)
init_auth(app)   # /api/login, /api/change-password, /api/logout, /api/me + session checks


# Serve the existing staff frontend
@app.route("/")
def index():
    return send_from_directory(PROJECT_DIR, FRONTEND_FILE)


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
    verify_database()
    app.run(host="127.0.0.1", port=5000, debug=True)
