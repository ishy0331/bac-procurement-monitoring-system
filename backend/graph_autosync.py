"""
backend/graph_autosync.py - Keep the Neo4j knowledge graph in step with MariaDB automatically.

Why
  MariaDB is the source of truth. Before this module, Neo4j was only updated when the Super Admin
  pressed "Sync" (POST /api/graph/neo4j/sync), so a record encoded in the Procurement Encoding tab
  never reached the knowledge graph on its own.

What it does
  * After every successful procurement write (create, edit, archive, restore) procurement_api.py
    calls request_sync(). A single background worker then runs graph_sync.run_sync() - the same,
    already-tested synchronization the Super Admin button uses (one Neo4j write transaction, owned
    data only, read-back verification).
  * Requests that arrive while a sync is running are merged into ONE follow-up run, so a burst of
    saves never queues up many syncs.
  * If Neo4j is down or a sync fails, the worker keeps retrying with a growing delay (5 s up to
    5 min) until it succeeds. Nothing is lost: the records are already committed in MariaDB, and
    the next successful sync copies everything that is missing.
  * One catch-up sync runs when the server starts, so records saved while Neo4j was stopped (or
    while the server was off) are added as soon as both are running again.
  * Nothing here writes to MariaDB.

Turn it off (for example for maintenance) with AUTO_GRAPH_SYNC = False in backend/config.py.
"""

import logging
import threading
import time
from datetime import datetime, timezone

_log = logging.getLogger(__name__)

RETRY_DELAYS = (5, 15, 30, 60, 120, 300)     # seconds between attempts after a failure
BUSY_DELAY = 2                               # a manual sync is running: try again shortly

_cond = threading.Condition()
_state = {
    "requested": 0,         # ticket of the newest request
    "covered": 0,           # newest ticket included in a SUCCESSFUL sync
    "attempted": 0,         # newest ticket included in a finished attempt (success or failure)
    "running": False,
    "lastStatus": None,     # "success" | "error"
    "lastMessage": None,
    "lastFinishedAt": None,
    "lastSuccessAt": None,
    "failures": 0,          # consecutive failed attempts
}
_thread = None
_enabled = True


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _enabled_in_config():
    try:
        import config
        return bool(getattr(config, "AUTO_GRAPH_SYNC", True))
    except Exception:
        return True


def start(initial_sync=True):
    """Start the background worker once per process (safe to call more than once)."""
    global _thread, _enabled
    _enabled = _enabled_in_config()
    if not _enabled:
        _log.info("Automatic knowledge-graph sync is turned off (AUTO_GRAPH_SYNC = False).")
        return
    with _cond:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_worker, name="graph-autosync", daemon=True)
        _thread.start()
    if initial_sync:
        request_sync("server start (catch-up)")


def request_sync(reason=""):
    """Ask for a sync that includes every MariaDB change committed before this call.
    Returns a ticket for wait_for(), or None when automatic sync is off."""
    if not _enabled or _thread is None:
        return None
    with _cond:
        _state["requested"] += 1
        ticket = _state["requested"]
        _cond.notify_all()
    _log.info("Knowledge-graph sync requested (%s), ticket %s", reason or "change", ticket)
    return ticket


def wait_for(ticket, timeout):
    """Wait up to `timeout` seconds for the first attempt that includes `ticket`.
    Returns a small dict for the API response (never raises)."""
    if ticket is None:
        return {"synced": False, "pending": False, "enabled": False,
                "message": "Automatic knowledge-graph sync is turned off. "
                           "The Super Admin can run Sync Now to update the graph."}
    deadline = time.monotonic() + timeout
    with _cond:
        while _state["attempted"] < ticket:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _cond.wait(remaining)
        if _state["covered"] >= ticket:
            return {"synced": True, "pending": False, "enabled": True,
                    "message": "Added to the knowledge graph (Neo4j)."}
        if _state["attempted"] >= ticket:
            return {"synced": False, "pending": True, "enabled": True,
                    "message": "Saved in the database. The knowledge graph could not be updated yet ("
                               + (_state["lastMessage"] or "Neo4j error")
                               + "). It will be updated automatically when Neo4j is available."}
        return {"synced": False, "pending": True, "enabled": True,
                "message": "Saved in the database. The knowledge graph is being updated in the background."}


def status():
    """Snapshot of the worker state (for the Super Admin)."""
    with _cond:
        s = dict(_state)
    s["enabled"] = _enabled and _thread is not None
    s["upToDate"] = s["covered"] >= s["requested"]
    return s


def _worker():
    import graph_sync                       # imported here: keeps app start-up independent of Neo4j

    while True:
        with _cond:
            while _state["attempted"] >= _state["requested"] and _state["covered"] >= _state["requested"]:
                _cond.wait()
            target = _state["requested"]    # everything requested so far is covered by this run
            _state["running"] = True

        try:
            code, body = graph_sync.run_sync()
        except Exception as exc:            # run_sync never raises, but stay alive regardless
            code, body = 500, {"status": "error", "message": f"Unexpected error ({type(exc).__name__})."}

        ok = code == 200 and body.get("status") == "success"
        busy = (code == 409 and body.get("stage") == "start")     # a manual sync holds the lock
        message = body.get("message") or ("HTTP %s" % code)

        with _cond:
            _state["running"] = False
            if busy:
                pass                         # not an attempt: retry shortly without reporting failure
            else:
                _state["attempted"] = max(_state["attempted"], target)
                _state["lastStatus"] = "success" if ok else "error"
                _state["lastMessage"] = message
                _state["lastFinishedAt"] = _now()
                if ok:
                    _state["covered"] = max(_state["covered"], target)
                    _state["lastSuccessAt"] = _state["lastFinishedAt"]
                    _state["failures"] = 0
                else:
                    _state["failures"] += 1
            failures = _state["failures"]
            _cond.notify_all()

        if busy:
            time.sleep(BUSY_DELAY)
        elif ok:
            totals = body.get("totals") or {}
            _log.info("Knowledge-graph sync done (ticket %s): %s", target, totals)
        else:
            delay = RETRY_DELAYS[min(failures, len(RETRY_DELAYS)) - 1]
            _log.warning("Knowledge-graph sync failed (ticket %s, HTTP %s, stage %s): %s - retrying in %ss",
                         target, code, body.get("stage"), message, delay)
            time.sleep(delay)
            with _cond:
                # Retry: make the loop run again even if no new request arrived.
                if _state["covered"] < _state["requested"]:
                    _state["attempted"] = min(_state["attempted"], _state["covered"])
