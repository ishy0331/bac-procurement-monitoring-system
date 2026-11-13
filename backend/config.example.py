"""
backend/config.example.py - Template for backend/config.py.

Copy this file to backend/config.py and fill in your own values:

    copy backend\\config.example.py backend\\config.py      (PowerShell / cmd)

backend/config.py is listed in .gitignore. Never commit it and never paste it
into chats or documents: it holds the database password and SECRET_KEY.
"""

# MariaDB connection (used by backend/db.py).
DB_CONFIG = {
    "host": "127.0.0.1",
    "port": 3306,                                  # development laptop: 3306
    "user": "root",                                # better: a dedicated app user
    "password": "CHANGE-ME",
    "database": "procurement_db_frontend_test",    # app.py refuses any other name
}

# Signs the login session cookie. Must be at least 32 characters, or the app
# will not start. Generate a new one for every installation with:
#   python -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY = "CHANGE-ME"

# True only when the site is served over HTTPS (it cannot work on http://127.0.0.1).
SESSION_COOKIE_SECURE = False

# Neo4j settings are NOT read from this file. Set them as environment variables
# (see README.md): NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE.
