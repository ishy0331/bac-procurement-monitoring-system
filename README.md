# BAC Procurement Monitoring System

Knowledge Graph-Based Procurement Monitoring and Supplier Visualization System for Santa Rosa City.

A Flask backend serves a single-page frontend (`proctest19.html`). MariaDB is the authoritative database; Neo4j holds a synchronized copy of the procurement graph for visualization.

## Requirements

| Component | Development version |
|---|---|
| Windows | 10/11 |
| Python | 3.14.5 |
| MariaDB | 13.0.2 (12.3 LTS recommended for deployment) |
| Neo4j | 2026.10.1 (via Neo4j Desktop 2) |
| HeidiSQL (optional, DB admin) | 12.22 |

Python packages are pinned in `requirements.txt`.

The frontend loads Tailwind CSS and Font Awesome from the internet, so the browser needs an internet connection to display the pages correctly.

## Setup (PowerShell)

1. **Clone and enter the project**
   ```powershell
   git clone https://github.com/ishy0331/bac-procurement-monitoring-system.git
   cd bac-procurement-monitoring-system
   ```

2. **Create and activate the virtual environment**
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
   If PowerShell says running scripts is disabled, run once:
   `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`

3. **Create the database**
   In MariaDB (e.g. HeidiSQL), create `procurement_db_frontend_test` and run the schema `schema_proposal_v4.sql` on it.

4. **Create `backend/config.py`**
   ```powershell
   copy backend\config.example.py backend\config.py
   ```
   Fill in the MariaDB port, user and password, and a new `SECRET_KEY`:
   ```powershell
   python -c "import secrets; print(secrets.token_hex(32))"
   ```

5. **Load the fictional sample data** (optional)
   ```powershell
   python backend\seed.py --dry-run     # test only, saves nothing
   python backend\seed.py               # insert sample data
   ```
   The script prints temporary passwords for the 3 demo accounts. They must be changed at first sign-in.

6. **Set the Neo4j environment variables** (needed only for the knowledge graph sync)
   ```powershell
   $env:NEO4J_URI = "neo4j://127.0.0.1:7687"
   $env:NEO4J_USERNAME = "neo4j"
   $env:NEO4J_PASSWORD = "your-neo4j-password"
   $env:NEO4J_DATABASE = "neo4j"
   ```
   These last only for the current PowerShell window. Without them the app still runs; the graph sync reports "not configured".

7. **Check the database (read-only)**
   ```powershell
   python backend\check_phase2_db.py
   python backend\check_phase3_prereqs.py
   ```

8. **Run the app**
   ```powershell
   python backend\app.py
   ```
   Open http://127.0.0.1:5000. The app refuses to start if it is not connected to `procurement_db_frontend_test`.

## Tests

The graph tests use simulated databases and never touch MariaDB or Neo4j:
```powershell
cd backend
python t_graph_sync.py
python t_graph_store.py
```

## Before deployment

- Generate a new `SECRET_KEY` and use a dedicated database user with a strong password.
- Turn off `debug=True` in `backend/app.py` and serve the app with a production WSGI server.
- Serve over HTTPS and set `SESSION_COOKIE_SECURE = True`.

## Branches

- `main`: Phase 2 (authentication and access control).
- `phase3`: procurement editing, archive management, activity log export, sample City knowledge graph, Neo4j sync.
