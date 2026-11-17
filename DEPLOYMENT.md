# Deployment Guide

How to run the BAC Procurement Monitoring System for real users (the BAC staff
and the public portal) on a Windows PC: the development laptop or an office PC.

```
Browser ──► Waitress (backend/serve.py, port 8000) ──► MariaDB (port 3306)
   ▲                                                └─► Neo4j  (port 7687)
   └── website: Tailscale Funnel (fixed https://…ts.net address)
```

| | Development (`backend\app.py`) | Deployment (`backend\serve.py`) |
|---|---|---|
| Server | Flask built-in server | Waitress (production WSGI server) |
| Address | http://127.0.0.1:5000 | http://localhost:8000, office network, or website (https://…ts.net) |
| Debug | optional (`DEBUG` in config.py) | refused |
| Database user | root | `bac_app` (SELECT/INSERT/UPDATE/DELETE only) |
| Frontend assets | local `vendor/` | local `vendor/` (no internet needed) |

---

## 1. One-time setup

Do these in PowerShell, in the project folder, with the virtual environment active
(`.\.venv\Scripts\Activate.ps1`).

### 1.1 Install Waitress
```powershell
pip install -r requirements.txt
pip show waitress        # note the version, then pin it in requirements.txt (waitress==X.Y.Z)
```

### 1.2 Download the frontend files (needs internet, once)
```powershell
.\deploy\get_vendor_assets.ps1
```
This saves Tailwind and Font Awesome into `vendor\`. Commit the `vendor\` folder
so any fresh clone also works offline.

### 1.3 Create the dedicated database user
1. Open `deploy\create_app_user.sql`, replace both `CHANGE-ME-strong-password`
   with one strong password. Generate one with:
   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(24))"
   ```
2. Run the file in HeidiSQL, connected as **root**.
3. Undo the password edit in the .sql file (do not commit or keep the real password there).

Keep using root for `seed.py` and schema changes; the app itself uses `bac_app`.

### 1.4 Update `backend\config.py`
Compare with `backend\config.example.py` and set:

```python
DB_CONFIG = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "bac_app",
    "password": "<the bac_app password>",
    "database": "procurement_db_frontend_test",
}
SECRET_KEY = "<NEW key>"     # python -c "import secrets; print(secrets.token_hex(32))"
SESSION_COOKIE_SECURE = False   # True for the website setup (section 4)
DEBUG = False
HOST = "0.0.0.0"     # office network; "127.0.0.1" for tunnel-only
PORT = 8000
THREADS = 8
BEHIND_PROXY = False  # True for the website setup (section 4)
```

The development `SECRET_KEY` was shared in a chat, so a **new** one is required.
Changing it signs everyone out once; that is expected.

### 1.5 Neo4j settings
```powershell
copy deploy\local_env.example.ps1 deploy\local_env.ps1
notepad deploy\local_env.ps1      # set NEO4J_PASSWORD
```
`deploy\local_env.ps1` is git-ignored and loaded by the start script.

### 1.6 Keep Neo4j running
- **Simplest (evaluation period):** start the database in Neo4j Desktop and leave
  Desktop open while the system is in use.
- **Unattended PC:** install Neo4j Server (Community Edition) and register it as a
  Windows service from an Administrator PowerShell in its folder:
  `bin\neo4j windows-service install`, then `bin\neo4j start`. Check the Neo4j
  Operations Manual for your exact version.

MariaDB already runs as a Windows service.

---

## 2. Start the system

```powershell
.\deploy\start_server.ps1
```
Expected output:
```
Database check passed: using 'procurement_db_frontend_test'.
Serving on http://0.0.0.0:8000  (Waitress, 8 threads). Press Ctrl+C to stop.
```
Open http://localhost:8000 on the same PC. Stop with **Ctrl+C**.

Quick checks:
- http://localhost:8000/api/health → `"status": "success"`
- Sign in as a staff account; open the registry and the knowledge graph page.
- Icons and layout look normal **with Wi-Fi turned off** (proves the vendor files work).

---

## 3. Office network access (LAN)

Requires `HOST = "0.0.0.0"` in config.py.

1. Find this PC's IP address: `ipconfig` → **IPv4 Address** of the Wi-Fi/Ethernet
   adapter (e.g. `192.168.1.25`).
2. Allow the port through Windows Firewall (Administrator PowerShell, once):
   ```powershell
   New-NetFirewallRule -DisplayName "BAC Procurement System (8000)" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private
   ```
   The Wi-Fi network must be set to **Private** in Windows settings.
3. Other PCs open `http://192.168.1.25:8000`.

Notes:
- The IP can change when the PC reconnects. If users can't connect, re-check `ipconfig`.
  Ask the office IT to reserve the IP (DHCP reservation) if the system stays there.
- This is plain HTTP, readable on the local network. Acceptable for an internal
  evaluation; use the tunnel (section 4) or a proper server with HTTPS for real use.

---

## 4. Put it on a website (Tailscale Funnel, fixed free address)

This publishes the system at a fixed HTTPS address such as
`https://bac-procurement.tail1234.ts.net` that anyone can open from any browser.
Visitors do not install anything. It is free, the address does not change, there
is no warning page in front of the site, and the laptop's IP address stays hidden.
The laptop is still the server: **the website is up only while the laptop is on,
awake, online, and the system is running.**

### 4.1 One-time setup
1. Install Tailscale from https://tailscale.com/download (or
   `winget install --id Tailscale.Tailscale`) and sign in (a Google/Microsoft/GitHub
   account works; the free Personal plan includes Funnel).
2. **Choose the address name** (optional, before sharing the link):
   - Machine name: Tailscale admin console (https://login.tailscale.com/admin/machines)
     → this laptop → **⋯ → Edit machine name** → e.g. `bac-procurement`.
   - Tailnet name (the `tail1234` part): admin console → **DNS** → Tailnet name
     (you can pick a randomly generated friendlier name there).
   Changing either later changes the address, so decide before giving the link out.
3. In `backend\config.py` set:
   ```python
   HOST = "127.0.0.1"           # only Tailscale reaches Waitress, not the network
   BEHIND_PROXY = True
   SESSION_COOKIE_SECURE = True # cookie only over HTTPS
   ```
4. Start the system: `.\deploy\start_server.ps1`.
5. In an **Administrator** PowerShell (once):
   ```powershell
   tailscale funnel --bg 8000
   ```
   (use your `PORT` from config.py if it is not 8000). The first time, it prints a
   link to enable HTTPS certificates and Funnel for your tailnet: open it, approve,
   then run the command again. It then shows:
   ```
   Available on the internet:
   https://bac-procurement.tail1234.ts.net/
   |-- / proxy http://127.0.0.1:8000
   ```
   That is your website address. The first visit can take up to about 10 minutes to
   work while the public DNS record appears.

`--bg` makes the Funnel setting permanent: Tailscale runs as a Windows service, so
after a restart the address comes back by itself. Only the system itself
(`start_server.ps1`) has to be started again; see section 5 to automate that.

### 4.2 Day to day
- Check: `tailscale funnel status`.
- Turn the website off: `tailscale funnel --https=443 off` (Administrator).
- Test from outside: open the link on a phone using **mobile data, not the office Wi-Fi**.

### 4.3 Things to know
- With `SESSION_COOKIE_SECURE = True`, sign-in works **only** through the https
  address, not `http://localhost:8000` or a LAN address. Everyone, including office
  staff, uses the https link. For LAN-only use, set the three settings back
  (section 1.4).
- The staff sign-in page is also reachable from the internet. Accounts lock after
  repeated wrong passwords, but make sure every account has a strong password and
  that unused demo accounts are disabled.
- Windows power settings: **never sleep** when plugged in, and closing the lid
  does nothing. Keep the laptop plugged in.
- The activity log may show `127.0.0.1` instead of the visitor's IP address when
  Tailscale does not pass it on. That does not affect anything else.

### 4.4 Alternative: Cloudflare quick tunnel (random address)
If Tailscale is not possible:
```powershell
winget install --id Cloudflare.cloudflared
cloudflared tunnel --url http://localhost:8000
```
It prints a `https://<random-words>.trycloudflare.com` address that changes every
time cloudflared restarts. Same config.py settings as 4.1 step 3. A fixed
Cloudflare address needs your own domain (about USD 10/year).

---

## 5. Optional: start automatically when Windows starts

Task Scheduler → **Create Task**:
- General: "Run whether user is logged on or not".
- Trigger: **At startup**.
- Action: Program `powershell.exe`, arguments
  `-ExecutionPolicy Bypass -File "C:\Users\acer\bac-procurement-monitoring-system\deploy\start_server.ps1"`.
- Neo4j must also start by itself (Neo4j Server service, section 1.6).

---

## 6. Backups

Before the evaluation and regularly during it (as root; the `bin` folder of
MariaDB must be on PATH, or use HeidiSQL → Export database as SQL):
```powershell
mariadb-dump -u root -p --routines --triggers --single-transaction procurement_db_frontend_test > backup_$(Get-Date -Format yyyyMMdd_HHmm).sql
```
Neo4j holds a **copy** of the MariaDB data; after restoring MariaDB, run the graph
sync again instead of backing up Neo4j.

Keep backups off the laptop too (USB drive or the group's shared drive). They contain
user password hashes: do not upload them anywhere public.

---

## 7. Checklist before the evaluation

- [ ] New `SECRET_KEY` in config.py
- [ ] App connects as `bac_app`, not root; root has a strong password
- [ ] `DEBUG = False`; started with `start_server.ps1` (Waitress), not `app.py`
- [ ] Pages look right with internet turned off
- [ ] `python backend\check_phase2_db.py` and `check_phase3_prereqs.py` pass
- [ ] Each respondent has an account and has changed the temporary password
- [ ] Graph sync works (Neo4j running, `local_env.ps1` set)
- [ ] Database backup taken
- [ ] PC set not to sleep; firewall rule added (LAN) or website link tested from a phone on mobile data

---

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Waitress is not installed` | Activate `.venv`, run `pip install -r requirements.txt`. |
| `Refusing to start: DEBUG = True` | Set `DEBUG = False` in config.py. |
| `Refusing to start: connected to '...'` | `DB_CONFIG["database"]` must be `procurement_db_frontend_test`. |
| `Access denied for user 'bac_app'` | Password in config.py differs from the one in the SQL script, or the script was not run. Re-run `ALTER USER 'bac_app'@'localhost' IDENTIFIED BY '...';` (and for `'bac_app'@'127.0.0.1'`) as root. |
| `SELECT command denied` / `INSERT command denied` | Grants missing: re-run `deploy\create_app_user.sql`. |
| `Can't connect to MySQL server on '127.0.0.1:3306'` | MariaDB service stopped: Services → MariaDB → Start. |
| Error 1267 "Illegal mix of collations" | A table uses `utf8mb4_uca1400_ai_ci`. Convert it: `ALTER TABLE <t> CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;` |
| `[Errno 10048]` / address already in use | Another program uses the port. Close the other server window, or change `PORT`. |
| Pages have no styling / squares instead of icons | `vendor\` files missing: run `.\deploy\get_vendor_assets.ps1`. |
| Graph sync says "not configured" | `deploy\local_env.ps1` missing or not filled in. |
| Graph sync: Neo4j unavailable | Neo4j not running: start it in Neo4j Desktop or `bin\neo4j start`. |
| Other PCs can't open the site | `HOST = "0.0.0.0"`? Firewall rule added? Network set to Private? Same network? Current IP from `ipconfig`? |
| Sign-in "works" but you are immediately signed out | `SESSION_COOKIE_SECURE = True` while using an http:// address. Use the https tunnel link or set it to False. |
| Tunnel shows **502 Bad Gateway** | Waitress is not running, or not on port 8000. Start `start_server.ps1` first. |
| `OSError: [WinError 10048]` or "address already in use" when starting | Another program uses that port (on the BAC laptop, an EnterpriseDB/PostgreSQL web server holds 8080). Set `PORT` in `backend/config.py` to a free port, e.g. `PORT = 8001`. Check a port with `netstat -ano | findstr :8000`. |
| Tunnel link stopped working | cloudflared was closed or restarted (new random address). Run it again and share the new link. |
| Website (ts.net) does not open | Is the laptop on, awake and online? Is `start_server.ps1` running? `tailscale funnel status` shows the address? Just set up: wait ~10 minutes for DNS. |
| ts.net site shows a connection/502 error | Waitress not running or on a different port than the Funnel target. Check `PORT` and re-run `tailscale funnel --bg <PORT>`. |
| `tailscale funnel` says Funnel/HTTPS not enabled | Open the link it prints, approve in the admin console, run the command again. |
