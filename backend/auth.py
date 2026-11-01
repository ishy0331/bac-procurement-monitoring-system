"""
backend/auth.py - Phase 2 authentication for the Procurement Monitoring System.

Provides:
  POST /api/login            username + password + role (from the login dropdown)
  POST /api/change-password  current + new + confirm (also activates pending accounts)
  POST /api/logout
  GET  /api/me               who is signed in (lets a page refresh stay signed in)
  require_role(...)          decorator for protected routes (401 / 403)

Security rules
  * Passwords: Werkzeug scrypt hashes only; plain text is never stored or logged.
  * Login: one generic error for every failure; the role chosen on the login
    screen must equal the role stored in the database; 5 failures lock the
    account for 15 minutes; Disabled accounts are always refused.
  * 'Pending activation' accounts get a RESTRICTED session (10 minutes) that can
    only call /api/change-password, /api/logout and /api/me. Every other /api/
    endpoint answers 403 password_change_required - including endpoints that are
    open to anonymous public visitors, such as GET /api/public/procurements. A new
    /api/login attempt first ends the restricted session (it never uses it).
  * Sessions: signed cookie; idle timeout 15 minutes; absolute limit 8 hours.
    Every /api/ request re-checks the user in the database: if the account was
    disabled, its role changed, or its password changed (password fingerprint),
    the old session is cleared at once.
  * Every POST/PUT/PATCH/DELETE under /api/ must carry X-Requested-With: fetch.
"""

import hashlib
import hmac
import re
import secrets
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, current_app, g, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from db import get_db_connection

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
IDLE_TIMEOUT_SECONDS = 15 * 60          # manuscript: 15 minutes of inactivity
ABSOLUTE_TIMEOUT_SECONDS = 8 * 60 * 60  # sign in again at least every 8 hours
PENDING_TIMEOUT_SECONDS = 10 * 60       # time allowed to set the new password
MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15

PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_LENGTH = 128

ROLE_LABELS = {"secretariat": "BAC Secretariat", "member": "BAC Member", "admin": "Super Admin"}
LABEL_TO_ROLE = {label: key for key, label in ROLE_LABELS.items()}

GENERIC_LOGIN_ERROR = ("Invalid username, password or role, "
                       "or the account is temporarily locked.")
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# The only endpoints a 'Pending activation' (restricted) session may call.
PENDING_ALLOWED_ENDPOINTS = {"auth.change_password", "auth.logout", "auth.me"}

# Used when the username does not exist, so a wrong username takes as long to
# reject as a wrong password (no timing difference to probe usernames).
_DUMMY_HASH = generate_password_hash(secrets.token_hex(16), method="scrypt")

# Clock function (replaceable in automated tests).
_clock = time.time

auth_bp = Blueprint("auth", __name__)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _json_error(message, status, **extra):
    body = {"status": "error", "message": message}
    body.update(extra)
    return jsonify(body), status


def _json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def _public_user(row):
    return {"username": row["username"], "full_name": row["full_name"],
            "role": row["role"], "roleLabel": ROLE_LABELS.get(row["role"], row["role"])}


def _password_fingerprint(password_hash):
    """Short keyed fingerprint of the stored hash. It changes whenever the
    password changes, which invalidates every session created before."""
    key = current_app.secret_key.encode("utf-8")
    return hmac.new(key, password_hash.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _start_session(row, pending):
    """Clear any old session data, then create a new one (no session reuse)."""
    now = _clock()
    session.clear()
    session["auth"] = {
        "uid": row["user_id"],
        "role": row["role"],
        "pwf": _password_fingerprint(row["password_hash"]),
        "pending": bool(pending),
        "auth_at": now,
        "last_seen": now,
    }


def _log(cur, row, action, detail, tone="info"):
    """Append an entry to activity_log (inside the caller's transaction)."""
    actor = f"{ROLE_LABELS.get(row['role'], row['role'])} ({row['username']})"
    cur.execute("INSERT INTO activity_log (user_id, actor_label, action, detail, tone, "
                "entity_type, entity_id, ip_address) "
                "VALUES (%s, %s, %s, %s, %s, 'user', %s, %s)",
                (row["user_id"], actor[:100], action, detail, tone,
                 row["user_id"], request.remote_addr))


def _lock_user_row(cur, *, username=None, user_id=None):
    """Read one user and lock the row until COMMIT/ROLLBACK."""
    sql = ("SELECT user_id, username, full_name, role, status, password_hash, "
           "failed_login_count, locked_until FROM users WHERE ")
    if username is not None:
        cur.execute(sql + "username = %s FOR UPDATE", (username,))
    else:
        cur.execute(sql + "user_id = %s FOR UPDATE", (user_id,))
    return cur.fetchone()


def _record_failure(cur, row, now_dt):
    """Count a failed attempt; lock the account on the 5th. Returns True if locked."""
    failures = (row["failed_login_count"] or 0) + 1
    if failures >= MAX_FAILED_LOGINS:
        cur.execute("UPDATE users SET failed_login_count = 0, locked_until = %s "
                    "WHERE user_id = %s",
                    (now_dt + timedelta(minutes=LOCK_MINUTES), row["user_id"]))
        _log(cur, row, "Account locked",
             f"Locked for {LOCK_MINUTES} minutes after {MAX_FAILED_LOGINS} failed attempts.",
             "warn")
        return True
    cur.execute("UPDATE users SET failed_login_count = %s WHERE user_id = %s",
                (failures, row["user_id"]))
    return False


def password_problems(new_password, confirm_password, username, current_hash):
    """Return {field: message} for every rule the new password breaks."""
    errors = {}
    if not isinstance(new_password, str) or not new_password:
        errors["new_password"] = "Enter a new password."
        return errors
    rules = []
    if len(new_password) < PASSWORD_MIN_LENGTH:
        rules.append(f"at least {PASSWORD_MIN_LENGTH} characters")
    if len(new_password) > PASSWORD_MAX_LENGTH:
        rules.append(f"at most {PASSWORD_MAX_LENGTH} characters")
    if not re.search(r"[A-Za-z]", new_password) or not re.search(r"\d", new_password):
        rules.append("at least one letter and one digit")
    if username and username.lower() in new_password.lower():
        rules.append("must not contain your username")
    if current_hash and check_password_hash(current_hash, new_password):
        rules.append("must differ from your current password")
    if rules:
        errors["new_password"] = "New password: " + "; ".join(rules) + "."
    if new_password != confirm_password:
        errors["confirm_password"] = "The new password and its confirmation do not match."
    return errors


# ---------------------------------------------------------------------------
# Runs before every request
# ---------------------------------------------------------------------------
def _is_api_path():
    """True for /api and everything under /api/ (the bare /api route too)."""
    return request.path == "/api" or request.path.startswith("/api/")


def _check_request_header():
    if (_is_api_path() and request.method in UNSAFE_METHODS
            and request.headers.get("X-Requested-With") != "fetch"):
        return _json_error("Missing required request header.", 400)
    return None


def _load_session_user():
    """Validate the session for /api/ requests and set g.user / g.pending_user."""
    g.user = None
    g.pending_user = None
    g.session_expired = False
    if not _is_api_path():
        return None

    auth = session.get("auth")
    if not isinstance(auth, dict):
        return None

    now = _clock()
    pending = bool(auth.get("pending"))

    if pending and request.endpoint == "auth.login":
        # A new sign-in attempt ends the restricted session first, so login()
        # runs exactly as it does for an anonymous visitor.
        session.clear()
        return None

    idle_limit = PENDING_TIMEOUT_SECONDS if pending else IDLE_TIMEOUT_SECONDS
    total_limit = PENDING_TIMEOUT_SECONDS if pending else ABSOLUTE_TIMEOUT_SECONDS
    if (now - float(auth.get("last_seen", 0)) > idle_limit
            or now - float(auth.get("auth_at", 0)) > total_limit):
        session.clear()
        g.session_expired = True
        return None

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True, buffered=True)
        cur.execute("SELECT user_id, username, full_name, role, status, password_hash "
                    "FROM users WHERE user_id = %s", (auth.get("uid"),))
        row = cur.fetchone()
        cur.close()
    except Exception:
        current_app.logger.exception("Session check failed")
        return _json_error("The server could not verify your session. Please try again.", 503)
    finally:
        if conn is not None:
            conn.close()

    expected_status = "Pending activation" if pending else "Active"
    if (row is None
            or row["status"] != expected_status
            or row["role"] != auth.get("role")
            or _password_fingerprint(row["password_hash"]) != auth.get("pwf")):
        # Disabled, role changed, password changed or account removed.
        session.clear()
        g.session_expired = True
        return None

    if pending and request.endpoint not in PENDING_ALLOWED_ENDPOINTS:
        # Restricted session: blocked from everything else, including public
        # endpoints (request.endpoint is None for unknown paths -> also blocked).
        return _json_error("Set a new password to activate your account first.", 403,
                           code="password_change_required")

    auth["last_seen"] = now
    session["auth"] = auth
    if pending:
        g.pending_user = _public_user(row)
    else:
        g.user = _public_user(row)
        g.user["user_id"] = row["user_id"]
    return None


def init_auth(app):
    """Register the auth routes and the per-request checks on the Flask app."""
    app.register_blueprint(auth_bp)
    app.before_request(_check_request_header)
    app.before_request(_load_session_user)


def require_role(*allowed_roles):
    """Protect a route: 401 if not signed in, 403 if the role is not allowed."""
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if g.get("pending_user"):
                return _json_error("Password change required.", 403,
                                   code="password_change_required")
            if not g.get("user"):
                if g.get("session_expired"):
                    return _json_error("Your session expired. Please sign in again.", 401,
                                       code="session_expired")
                return _json_error("Please sign in.", 401, code="not_authenticated")
            if allowed_roles and g.user["role"] not in allowed_roles:
                return _json_error("You do not have permission to do this.", 403,
                                   code="forbidden")
            return view(*args, **kwargs)
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@auth_bp.route("/api/login", methods=["POST"])
def login():
    data = _json_body()
    if data is None:
        return _json_error("Request body must be JSON.", 400)

    username = data.get("username")
    password = data.get("password")
    role_input = data.get("role")
    if (not isinstance(username, str) or not isinstance(password, str)
            or not username.strip() or not password
            or len(username) > 50 or len(password) > 256):
        return _json_error(GENERIC_LOGIN_ERROR, 401)
    username = username.strip()
    role_key = None
    if isinstance(role_input, str):
        role_key = LABEL_TO_ROLE.get(role_input, role_input if role_input in ROLE_LABELS else None)

    conn = None
    try:
        conn = get_db_connection()
        conn.autocommit = False
        cur = conn.cursor(dictionary=True, buffered=True)
        row = _lock_user_row(cur, username=username)
        now_dt = datetime.now()

        if row is None:
            check_password_hash(_DUMMY_HASH, password)        # equal timing
            conn.rollback()
            return _json_error(GENERIC_LOGIN_ERROR, 401)

        if row["status"] == "Disabled" or (row["locked_until"] and row["locked_until"] > now_dt):
            check_password_hash(_DUMMY_HASH, password)        # equal timing
            conn.rollback()
            return _json_error(GENERIC_LOGIN_ERROR, 401)

        password_ok = check_password_hash(row["password_hash"], password)
        if not (password_ok and role_key == row["role"]):
            _record_failure(cur, row, now_dt)
            conn.commit()
            return _json_error(GENERIC_LOGIN_ERROR, 401)

        cur.execute("UPDATE users SET failed_login_count = 0, locked_until = NULL, "
                    "last_login_at = %s WHERE user_id = %s", (now_dt, row["user_id"]))
        pending = row["status"] == "Pending activation"
        _log(cur, row, "Login (password change required)" if pending else "Login",
             "Signed in with a temporary password." if pending else "Signed in.")
        conn.commit()

        _start_session(row, pending=pending)
        if pending:
            return jsonify({"status": "password_change_required",
                            "message": "Set a new password to activate your account.",
                            "user": _public_user(row)})
        return jsonify({"status": "success", "user": _public_user(row)})

    except Exception:
        if conn is not None:
            conn.rollback()
        current_app.logger.exception("Login failed with a server error")
        return _json_error("Sign-in is temporarily unavailable. Please try again.", 500)
    finally:
        if conn is not None:
            conn.close()


@auth_bp.route("/api/change-password", methods=["POST"])
def change_password():
    who = g.get("user") or g.get("pending_user")
    if not who:
        message = ("Your session expired. Please sign in again." if g.get("session_expired")
                   else "Please sign in.")
        return _json_error(message, 401)

    data = _json_body()
    if data is None:
        return _json_error("Request body must be JSON.", 400)
    current_pw = data.get("current_password")
    new_pw = data.get("new_password")
    confirm_pw = data.get("confirm_password")
    if not isinstance(current_pw, str) or not current_pw:
        return _json_error("Please correct the highlighted fields.", 400,
                           errors={"current_password": "Enter your current password."})

    conn = None
    try:
        conn = get_db_connection()
        conn.autocommit = False
        cur = conn.cursor(dictionary=True, buffered=True)
        row = _lock_user_row(cur, user_id=session["auth"]["uid"])
        if row is None:
            conn.rollback()
            session.clear()
            return _json_error("Please sign in again.", 401)

        if not check_password_hash(row["password_hash"], current_pw):
            locked = _record_failure(cur, row, datetime.now())
            conn.commit()
            if locked:
                session.clear()
                return _json_error(GENERIC_LOGIN_ERROR, 401)
            return _json_error("Please correct the highlighted fields.", 400,
                               errors={"current_password": "The current password is incorrect."})

        errors = password_problems(new_pw, confirm_pw, row["username"], row["password_hash"])
        if errors:
            conn.rollback()
            return _json_error("Please correct the highlighted fields.", 400, errors=errors)

        new_hash = generate_password_hash(new_pw, method="scrypt")
        was_pending = row["status"] == "Pending activation"
        cur.execute("UPDATE users SET password_hash = %s, status = 'Active', "
                    "failed_login_count = 0, locked_until = NULL WHERE user_id = %s",
                    (new_hash, row["user_id"]))
        _log(cur, row,
             "Password changed - account activated" if was_pending else "Password changed",
             "New password set by the account owner.")
        conn.commit()

        row["password_hash"] = new_hash
        row["status"] = "Active"
        _start_session(row, pending=False)     # new fingerprint: older sessions die
        return jsonify({"status": "success", "user": _public_user(row)})

    except Exception:
        if conn is not None:
            conn.rollback()
        current_app.logger.exception("Password change failed with a server error")
        return _json_error("Could not change the password. Nothing was changed.", 500)
    finally:
        if conn is not None:
            conn.close()


@auth_bp.route("/api/logout", methods=["POST"])
def logout():
    who = g.get("user") or g.get("pending_user")
    auth = session.get("auth") or {}
    if who:
        conn = None
        try:
            conn = get_db_connection()
            conn.autocommit = False
            cur = conn.cursor(dictionary=True, buffered=True)
            row = {"user_id": auth.get("uid"), "role": who["role"], "username": who["username"]}
            _log(cur, row, "Logout", "Signed out.")
            conn.commit()
        except Exception:
            if conn is not None:
                conn.rollback()
            current_app.logger.exception("Could not record the logout")
        finally:
            if conn is not None:
                conn.close()
    session.clear()
    return jsonify({"status": "success"})


@auth_bp.route("/api/me", methods=["GET"])
def me():
    if g.get("user"):
        user = {k: v for k, v in g.user.items() if k != "user_id"}
        return jsonify({"authenticated": True, "user": user})
    if g.get("pending_user"):
        return jsonify({"authenticated": False, "password_change_required": True,
                        "user": g.pending_user})
    return jsonify({"authenticated": False, "expired": bool(g.get("session_expired"))})
