"""
backend/serve.py - Run the system with Waitress (production WSGI server).

Use this instead of `python backend\\app.py` when other people will use the
system. Flask's built-in server (app.py) is for development only.

    python backend\\serve.py          (or deploy\\start_server.ps1)

Settings come from backend/config.py (all optional):

    HOST           "127.0.0.1" = this computer only (default; use with a tunnel)
                   "0.0.0.0"   = also reachable from other PCs on the office network
    PORT           default 8000 (8080 is taken by another program on the BAC laptop)
    THREADS        default 8
    BEHIND_PROXY   True when a Cloudflare Tunnel (or other reverse proxy) runs on
                   this same computer. Waitress then trusts X-Forwarded-For /
                   X-Forwarded-Proto ONLY from 127.0.0.1, so the activity log
                   records the visitor's real IP address instead of 127.0.0.1.
"""

import sys

try:
    from waitress import serve
except ImportError:
    sys.exit("Waitress is not installed. Run:  pip install -r requirements.txt")

import config
from app import app, verify_database

HOST = getattr(config, "HOST", "127.0.0.1")
PORT = int(getattr(config, "PORT", 8000))
THREADS = int(getattr(config, "THREADS", 8))
BEHIND_PROXY = bool(getattr(config, "BEHIND_PROXY", False))


def main():
    if getattr(config, "DEBUG", False):
        sys.exit("Refusing to start: DEBUG = True in backend/config.py. "
                 "Set DEBUG = False for deployment.")

    verify_database()   # exits unless connected to the expected database

    options = {"host": HOST, "port": PORT, "threads": THREADS}
    if BEHIND_PROXY:
        options.update(
            trusted_proxy="127.0.0.1",
            trusted_proxy_count=1,
            trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto"},
            clear_untrusted_proxy_headers=True,
        )

    shown_host = "localhost" if HOST in ("127.0.0.1", "localhost") else HOST
    print(f"Serving on http://{shown_host}:{PORT}  (Waitress, {THREADS} threads"
          f"{', behind proxy' if BEHIND_PROXY else ''}). Press Ctrl+C to stop.")
    if HOST == "0.0.0.0":
        print(f"Other PCs on the network: http://<this-PC's-IP-address>:{PORT}  "
              "(find the IP with:  ipconfig)")
    serve(app, **options)


if __name__ == "__main__":
    main()
