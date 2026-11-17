"""
backend/config.example.py - Template for backend/config.py.

Copy this file to backend/config.py and fill in your own values:

    copy backend\\config.example.py backend\\config.py      (PowerShell / cmd)

backend/config.py is listed in .gitignore. Never commit it and never paste it
into chats or documents: it holds the database password and SECRET_KEY.
"""

# MariaDB connection (used by backend/db.py).
# For deployment, use the dedicated app user from deploy/create_app_user.sql,
# not root.
DB_CONFIG = {
    "host": "127.0.0.1",
    "port": 3306,                                  # development laptop: 3306
    "user": "bac_app",                             # dedicated app user (not root)
    "password": "CHANGE-ME",
    "database": "procurement_db_frontend_test",    # app.py refuses any other name
}

# Signs the login session cookie. Must be at least 32 characters, or the app
# will not start. Generate a new one for every installation with:
#   python -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY = "CHANGE-ME"

# True only when the site is served over HTTPS (e.g. through a Cloudflare
# Tunnel). With True, sign-in does NOT work over plain http:// addresses.
SESSION_COOKIE_SECURE = False

# Development only. backend/serve.py refuses to start while this is True.
DEBUG = False

# ---- Waitress server (backend/serve.py) ----
# "127.0.0.1" = this computer only (use this with a Cloudflare Tunnel)
# "0.0.0.0"   = also reachable from other PCs on the office network
HOST = "127.0.0.1"
PORT = 8000                                    # 8080 is used by another program on the BAC laptop
THREADS = 8

# True when a Cloudflare Tunnel runs on this computer (records real visitor IPs).
BEHIND_PROXY = False

# Copy every saved procurement change (create / edit / archive / restore) into the Neo4j
# knowledge graph automatically, and run one catch-up sync when the server starts.
# Set to False only for maintenance; the Super Admin "Sync to Neo4j" button still works.
AUTO_GRAPH_SYNC = True

# Neo4j settings are NOT read from this file. Set them as environment variables
# (see README.md / deploy/local_env.example.ps1):
# NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE.

# ---- Document management (backend/documents_api.py) ----
# Uploaded files are stored here (NOT in a served folder). Back this folder up together with the database.
# Default: <project folder>\uploads. Create the table once with:  python backend\migrate_documents.py
# UPLOAD_DIR = r"C:\Users\acer\bac-procurement-monitoring-system\uploads"
MAX_UPLOAD_MB = 20
