"""
backend/admin_api.py - Super Admin: user accounts and system settings (saved to MariaDB).

Accounts (users table)
    GET  /api/admin/users                     every account
    POST /api/admin/users                     create: username, fullName, email, role
                                              -> 'Pending activation' + one-time temporary password (shown once)
    PUT  /api/admin/users/<id>                change full name, e-mail, role
    POST /api/admin/users/<id>/disable        sign-in refused; open sessions end at the next request
    POST /api/admin/users/<id>/enable         back to Active (or Pending activation if never signed in)
  Safety: the Super Admin cannot disable or demote their own account, and the last active Super Admin
  can never be disabled or demoted. Passwords are only stored as scrypt hashes. Every change is audited.

Settings (system_settings table)
    GET  /api/admin/settings                  current values + the procuring entities to choose from
    PUT  /api/admin/settings                  save the values below (only known keys are accepted)

    active_fiscal_year        4-digit year used for new reference numbers (SR-<year>-NNN)
    deadline_alert_days       dashboard "closing soon" lead time (0-30)
    require_philgeps_ref      1/0: a PhilGEPS reference is required to post a notice
    home_procuring_entity_id  the City's procuring entity (existing row, or a new official name)
    followup_*_days           dashboard follow-up thresholds (system rules, not statutory periods)
"""

import re

from flask import Blueprint, current_app, g, jsonify, request
from werkzeug.security import generate_password_hash

from auth import ROLE_LABELS, make_temporary_password, require_role
from procurement_api import ApiError, ValidationError, json_error, log_activity, normalize_key, run_write
from db import get_db_connection

admin_bp = Blueprint("admin", __name__)

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{3,50}$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

INT_SETTINGS = {                       # key -> (min, max, label)
    "deadline_alert_days": (0, 30, "Deadline alert lead time"),
    "followup_bids_days": (0, 365, "Follow-up: bids not recorded"),
    "followup_resolution_days": (0, 365, "Follow-up: no BAC resolution"),
    "followup_award_days": (0, 365, "Follow-up: no Notice of Award"),
    "followup_contract_days": (0, 365, "Follow-up: no contract"),
}
SETTING_DEFAULTS = {"active_fiscal_year": "2026", "deadline_alert_days": "3", "require_philgeps_ref": "1",
                    "home_procuring_entity_id": "", "followup_bids_days": "2", "followup_resolution_days": "15",
                    "followup_award_days": "15", "followup_contract_days": "10"}


def _iso_dt(value):
    return value.strftime("%Y-%m-%d %H:%M") if hasattr(value, "strftime") else (str(value)[:16] if value else None)


def _user_json(u, me):
    return {"userId": u["user_id"], "username": u["username"], "fullName": u["full_name"], "email": u["email"],
            "role": u["role"], "roleLabel": ROLE_LABELS.get(u["role"], u["role"]), "status": u["status"],
            "lastLoginAt": _iso_dt(u.get("last_login_at")), "isYou": u["user_id"] == me}


def _reply(fn, failure):
    try:
        result = run_write(fn)
    except ValidationError as exc:
        return json_error("Please correct the highlighted fields. Nothing was saved.", 400, errors=exc.errors)
    except ApiError as exc:
        return json_error(exc.message, exc.status, **exc.extra)
    except Exception as exc:
        current_app.logger.exception(failure)
        if getattr(exc, "errno", None) == 1062:
            return json_error("That username or e-mail is already used by another account. Nothing was saved.",
                              409, code="duplicate")
        return json_error(failure + " Nothing was saved.", 500)
    response = jsonify({"status": "success", **result})
    response.headers["Cache-Control"] = "no-store"
    return response


def _payload():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValidationError({"body": "Request body must be JSON."})
    return data


def _clean_account(data, errors, *, with_username):
    out = {}
    if with_username:
        username = str(data.get("username") or "").strip()
        if not USERNAME_PATTERN.match(username):
            errors["username"] = "Username: 3-50 letters, digits, dot, dash or underscore (no spaces)."
        out["username"] = username
    full_name = " ".join(str(data.get("fullName") or "").split())
    if not 2 <= len(full_name) <= 150:
        errors["fullName"] = "Enter the full name (2-150 characters)."
    email = str(data.get("email") or "").strip()
    if not EMAIL_PATTERN.match(email) or len(email) > 150:
        errors["email"] = "Enter a valid e-mail address."
    role = data.get("role")
    if role not in ROLE_LABELS:
        errors["role"] = "Select BAC Secretariat, BAC Member or Super Admin."
    out.update(fullName=full_name, email=email, role=role)
    return out


def _lock_user(cur, user_id):
    cur.execute("SELECT user_id, username, full_name, email, role, status, last_login_at FROM users "
                "WHERE user_id = %s FOR UPDATE", (user_id,))
    row = cur.fetchone()
    if row is None:
        raise ApiError("Account not found.", 404)
    return row


def _active_admins(cur):
    cur.execute("SELECT user_id FROM users WHERE role = 'admin' AND status = 'Active' FOR UPDATE")
    return {r["user_id"] for r in cur.fetchall()}


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
@admin_bp.route("/api/admin/users", methods=["GET"])
@require_role("admin")
def list_users():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True, buffered=True)
        cur.execute("SELECT user_id, username, full_name, email, role, status, last_login_at FROM users "
                    "ORDER BY CASE status WHEN 'Active' THEN 0 WHEN 'Pending activation' THEN 1 ELSE 2 END, role, username")
        users = [_user_json(u, g.user["user_id"]) for u in cur.fetchall()]
        cur.close()
    except Exception:
        current_app.logger.exception("Could not load the accounts")
        return json_error("Could not load the accounts.", 500)
    finally:
        if conn is not None:
            conn.close()
    response = jsonify({"status": "success", "users": users})
    response.headers["Cache-Control"] = "no-store"
    return response


@admin_bp.route("/api/admin/users", methods=["POST"])
@require_role("admin")
def create_user():
    def tx(cur):
        errors = {}
        a = _clean_account(_payload(), errors, with_username=True)
        if errors:
            raise ValidationError(errors)
        cur.execute("SELECT username, email FROM users WHERE username = %s OR email = %s", (a["username"], a["email"]))
        for row in cur.fetchall():
            if row["username"].lower() == a["username"].lower():
                errors["username"] = "This username is already used."
            if (row["email"] or "").lower() == a["email"].lower():
                errors["email"] = "This e-mail is already used by another account."
        if errors:
            raise ValidationError(errors)
        temp = make_temporary_password()
        cur.execute("INSERT INTO users (username, full_name, email, password_hash, role, status, failed_login_count, "
                    "locked_until, last_login_at) VALUES (%s, %s, %s, %s, %s, 'Pending activation', 0, NULL, NULL)",
                    (a["username"], a["fullName"], a["email"], generate_password_hash(temp, method="scrypt"), a["role"]))
        new_id = cur.lastrowid
        log_activity(cur, "Account created",
                     f"{a['username']} ({ROLE_LABELS[a['role']]}) created as Pending activation; a temporary "
                     "password was issued.", entity_type="user", entity_id=new_id, tone="ok")
        return {"message": f"Account {a['username']} created. Give the temporary password to the user; it is shown "
                           "only once.", "temporaryPassword": temp,
                "user": {"userId": new_id, "username": a["username"], "fullName": a["fullName"],
                         "roleLabel": ROLE_LABELS[a["role"]]}}
    return _reply(tx, "Could not create the account.")


@admin_bp.route("/api/admin/users/<int:user_id>", methods=["PUT"])
@require_role("admin")
def update_user(user_id):
    def tx(cur):
        errors = {}
        a = _clean_account(_payload(), errors, with_username=False)
        if errors:
            raise ValidationError(errors)
        admins = _active_admins(cur)
        row = _lock_user(cur, user_id)
        if a["role"] != row["role"]:
            if user_id == g.user["user_id"]:
                raise ApiError("You cannot change the role of your own account.", 409, code="own_account")
            if row["role"] == "admin" and admins == {user_id}:
                raise ApiError("This is the last active Super Admin; its role cannot be changed.", 409,
                               code="last_admin")
        cur.execute("SELECT user_id FROM users WHERE email = %s AND user_id <> %s", (a["email"], user_id))
        if cur.fetchone():
            raise ValidationError({"email": "This e-mail is already used by another account."})
        changes = [f"{label}: {old} -> {new}" for label, old, new in (
            ("name", row["full_name"], a["fullName"]), ("e-mail", row["email"], a["email"]),
            ("role", ROLE_LABELS.get(row["role"], row["role"]), ROLE_LABELS[a["role"]])) if old != new]
        if not changes:
            return {"message": "No changes."}
        cur.execute("UPDATE users SET full_name = %s, email = %s, role = %s WHERE user_id = %s",
                    (a["fullName"], a["email"], a["role"], user_id))
        log_activity(cur, "Account updated", f"{row['username']}: " + "; ".join(changes) + "."
                     + (" Open sessions of this user end (role changed)." if a["role"] != row["role"] else ""),
                     entity_type="user", entity_id=user_id)
        return {"message": f"Account {row['username']} updated."}
    return _reply(tx, "Could not update the account.")


@admin_bp.route("/api/admin/users/<int:user_id>/disable", methods=["POST"])
@require_role("admin")
def disable_user(user_id):
    def tx(cur):
        if user_id == g.user["user_id"]:
            raise ApiError("You cannot disable your own account.", 409, code="own_account")
        admins = _active_admins(cur)
        row = _lock_user(cur, user_id)
        if row["status"] == "Disabled":
            return {"message": f"{row['username']} is already disabled."}
        if row["role"] == "admin" and admins == {user_id}:
            raise ApiError("This is the last active Super Admin and cannot be disabled.", 409, code="last_admin")
        cur.execute("UPDATE users SET status = 'Disabled' WHERE user_id = %s", (user_id,))
        log_activity(cur, "Account disabled", f"{row['username']} disabled; sign-in is refused and open sessions end.",
                     entity_type="user", entity_id=user_id, tone="warn")
        return {"message": f"{row['username']} disabled."}
    return _reply(tx, "Could not disable the account.")


@admin_bp.route("/api/admin/users/<int:user_id>/enable", methods=["POST"])
@require_role("admin")
def enable_user(user_id):
    def tx(cur):
        row = _lock_user(cur, user_id)
        if row["status"] != "Disabled":
            return {"message": f"{row['username']} is already enabled."}
        status = "Active" if row["last_login_at"] else "Pending activation"
        cur.execute("UPDATE users SET status = %s, failed_login_count = 0, locked_until = NULL WHERE user_id = %s",
                    (status, user_id))
        log_activity(cur, "Account enabled", f"{row['username']} enabled ({status}).", entity_type="user",
                     entity_id=user_id, tone="ok")
        return {"message": f"{row['username']} enabled ({status})."}
    return _reply(tx, "Could not enable the account.")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def _read_settings(cur):
    cur.execute("SELECT setting_key, setting_value FROM system_settings")
    stored = {r["setting_key"]: r["setting_value"] for r in cur.fetchall()}
    values = {k: (stored.get(k) if stored.get(k) is not None else v) for k, v in SETTING_DEFAULTS.items()}
    cur.execute("SELECT entity_id, name, city, province FROM procuring_entities ORDER BY name")
    entities = [{"id": e["entity_id"], "name": e["name"], "city": e["city"], "province": e["province"]}
                for e in cur.fetchall()]
    return values, entities


@admin_bp.route("/api/admin/settings", methods=["GET"])
@require_role("admin")
def get_settings():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True, buffered=True)
        values, entities = _read_settings(cur)
        cur.close()
    except Exception:
        current_app.logger.exception("Could not load the settings")
        return json_error("Could not load the settings.", 500)
    finally:
        if conn is not None:
            conn.close()
    return jsonify({"status": "success", "settings": values, "entities": entities})


def _upsert(cur, key, value):
    cur.execute("SELECT setting_value FROM system_settings WHERE setting_key = %s FOR UPDATE", (key,))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO system_settings (setting_key, setting_value, updated_by) VALUES (%s, %s, %s)",
                    (key, value, g.user["user_id"]))
        return None, True
    if str(row["setting_value"]) == value:
        return row["setting_value"], False
    cur.execute("UPDATE system_settings SET setting_value = %s, updated_by = %s WHERE setting_key = %s",
                (value, g.user["user_id"], key))
    return row["setting_value"], True


@admin_bp.route("/api/admin/settings", methods=["PUT"])
@require_role("admin")
def save_settings():
    def tx(cur):
        data = _payload()
        errors, clean = {}, {}
        fy = str(data.get("active_fiscal_year", "")).strip()
        if not re.fullmatch(r"20\d{2}", fy):
            errors["active_fiscal_year"] = "Enter a 4-digit fiscal year (e.g. 2026)."
        clean["active_fiscal_year"] = fy
        for key, (lo, hi, label) in INT_SETTINGS.items():
            raw = str(data.get(key, SETTING_DEFAULTS[key])).strip()
            if not raw.isdigit() or not lo <= int(raw) <= hi:
                errors[key] = f"{label}: enter a whole number from {lo} to {hi}."
            clean[key] = str(int(raw)) if raw.isdigit() else raw
        clean["require_philgeps_ref"] = "1" if data.get("require_philgeps_ref") in (True, "1", 1) else "0"

        entity_id = data.get("home_procuring_entity_id")
        new_entity = " ".join(str(data.get("newEntityName") or "").split())
        entity_label = None
        if new_entity:
            if not 3 <= len(new_entity) <= 255:
                errors["newEntityName"] = "Enter the official name of the procuring entity (3-255 characters)."
        elif entity_id not in (None, ""):
            if not str(entity_id).isdigit():
                errors["home_procuring_entity_id"] = "Select a procuring entity from the list."
            else:
                cur.execute("SELECT entity_id, name FROM procuring_entities WHERE entity_id = %s", (int(entity_id),))
                row = cur.fetchone()
                if row is None:
                    errors["home_procuring_entity_id"] = "Select a procuring entity from the list."
                else:
                    clean["home_procuring_entity_id"], entity_label = str(row["entity_id"]), row["name"]
        if errors:
            raise ValidationError(errors)
        if new_entity:
            key = normalize_key(new_entity)
            cur.execute("SELECT entity_id, name FROM procuring_entities WHERE match_key = %s", (key,))
            row = cur.fetchone()
            if row is None:
                city = " ".join(str(data.get("newEntityCity") or "Santa Rosa City").split())[:100]
                province = " ".join(str(data.get("newEntityProvince") or "Laguna").split())[:100]
                cur.execute("INSERT INTO procuring_entities (name, match_key, city, province) VALUES (%s, %s, %s, %s)",
                            (new_entity, key, city, province))
                row = {"entity_id": cur.lastrowid, "name": new_entity}
            clean["home_procuring_entity_id"], entity_label = str(row["entity_id"]), row["name"]

        changed = []
        for key, value in clean.items():
            old, did = _upsert(cur, key, value)
            if did:
                shown = entity_label if key == "home_procuring_entity_id" else value
                changed.append(f"{key} = {shown}" + (f" (was {old})" if old is not None else ""))
        if changed:
            log_activity(cur, "System settings saved", "; ".join(changed) + ".", entity_type="settings", entity_id=None)
        values, entities = _read_settings(cur)
        return {"message": "Settings saved." if changed else "No changes.", "settings": values,
                "entities": entities, "changed": changed}
    return _reply(tx, "Could not save the settings.")
