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

Tailwind CSS and Font Awesome are served from the local `vendor/` folder (download once with `deploy\get_vendor_assets.ps1`), so the pages work without an internet connection.

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

7. **Install document management** (once; creates the table `procurement_documents`)
   ```powershell
   python backend\migrate_documents.py
   ```
   It asks for a MariaDB account that may create tables (normally `root`). Uploaded files are stored in the
   `uploads\` folder (or `UPLOAD_DIR` in config.py); back it up together with the database.

8. **Check the database (read-only)**
   ```powershell
   python backend\check_phase2_db.py
   python backend\check_phase3_prereqs.py
   ```

9. **Run the app**
   ```powershell
   python backend\app.py
   ```
   Open http://127.0.0.1:5000 (development server; for deployment see DEPLOYMENT.md). The app refuses to start if it is not connected to `procurement_db_frontend_test`.

## Tests

The graph tests use simulated databases and never touch MariaDB or Neo4j:
```powershell
cd backend
python t_graph_sync.py
python t_graph_store.py
python t_auth_crud_graph.py     # forgot password, procurement CRUD, encoded records -> knowledge graph
python t_features.py            # workflow steps, dashboard, accounts, settings, documents, Excel PMR
```

## Main modules

| File | What it does |
|---|---|
| `auth.py` | sign-in, sessions, password change, forgot password |
| `procurement_api.py` | create / edit / archive / restore procurement records |
| `workflow_api.py` | post ITB/RFQ, record bids, BAC resolution, Notice of Award, contract |
| `dashboard_api.py` | dashboard figures computed from the database |
| `admin_api.py` | Super Admin: user accounts and system settings |
| `documents_api.py` | document upload, download and public documents |
| `reports_api.py`, `xlsx_writer.py` | PMR as an Excel workbook (the PDF is printed from the browser) |
| `graph_api.py`, `graph_sync.py`, `graph_autosync.py`, `graph_store.py` | knowledge graph (MariaDB to Neo4j) |

## Knowledge graph updates

Records saved in the Procurement Encoding tab (`data_source = 'encoded'`) are part of the knowledge
graph together with the sample records. After every create, edit, archive or restore the server
copies MariaDB into Neo4j automatically (`backend/graph_autosync.py`); the page shows
"Added to the knowledge graph" when it is done. If Neo4j is stopped, the record is still saved in
MariaDB and the server keeps retrying until Neo4j is back; a catch-up sync also runs at every server
start. Super Admin: `GET /api/graph/neo4j/autosync` shows whether the graph is up to date.

## Forgot password

**Forgot password?** on the sign-in page sends a request to the Super Admin (no e-mail server is
needed). In **System Administration -> Password Resets** the Super Admin issues a one-time temporary
password (shown once, stored only as a hash) and gives it to the user, who must choose a new password
right after signing in with it. Every step is recorded in the activity log.

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md): Waitress server, dedicated database user, office-network access, optional Cloudflare Tunnel, backups and troubleshooting.

## Branches

- `main`: Phase 2 (authentication and access control).
- `phase3`: procurement editing, archive management, activity log export, sample City knowledge graph, Neo4j sync.
