"""
backend/neo4j_db.py - Connection to the Neo4j graph database (Step 1: connectivity only).

Neo4j will hold a COPY of the approved sample graph records. MariaDB stays the
authoritative database. In this step the module only opens the connection and runs
a health check (RETURN 1). It reads and writes no procurement data.

Settings come ONLY from environment variables, never from source code or config.py:

    NEO4J_URI        optional, default neo4j://127.0.0.1:7687
    NEO4J_USERNAME   required, no default
    NEO4J_PASSWORD   required, no default
    NEO4J_DATABASE   optional, default neo4j

Secrets policy:
  * The URI, username and password are never put in an API response or a log line.
  * Errors are reported by category (not_configured, unreachable, auth_failed, ...).
    Logs get the exception class name and the Neo4j status code only - never the
    exception text, which can contain server addresses.
  * A URI that carries a user name or password (neo4j://user:pass@host) is refused.

Requires the official Neo4j Python driver:  pip install "neo4j==6.4.1"
If the driver is not installed, the rest of the application still starts and the
health check reports "driver_missing".
"""

import atexit
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from urllib.parse import urlsplit

try:
    import neo4j
    from neo4j import GraphDatabase, RoutingControl
    from neo4j.exceptions import AuthError, Neo4jError, ServiceUnavailable
except ImportError:            # the application must still start without the driver
    neo4j = None

DEFAULT_URI = "neo4j://127.0.0.1:7687"
DEFAULT_DATABASE = "neo4j"
ALLOWED_SCHEMES = ("neo4j", "neo4j+s", "neo4j+ssc", "bolt", "bolt+s", "bolt+ssc")
# Neo4j database names: 3-63 characters, ASCII letters, digits, dots and dashes, starting with a letter.
DATABASE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{2,62}$")
TIMEOUT_SECONDS = 5.0          # connect / acquire / retry limits, so a request never hangs for long
DRIVER_INSTALL_HINT = 'pip install "neo4j==6.4.1"'

_log = logging.getLogger(__name__)


class Neo4jSettingsError(Exception):
    """Neo4j settings are missing or invalid. The message names variables only, never their values."""

    def __init__(self, message, missing=()):
        super().__init__(message)
        self.missing = tuple(missing)


@dataclass(frozen=True)
class Neo4jSettings:
    uri: str = field(repr=False)
    username: str = field(repr=False)
    password: str = field(repr=False)
    database: str


def read_settings(environ=None):
    """Read and validate the NEO4J_* environment variables. Raises Neo4jSettingsError."""
    env = os.environ if environ is None else environ
    uri = (env.get("NEO4J_URI") or "").strip() or DEFAULT_URI
    username = (env.get("NEO4J_USERNAME") or "").strip()
    password = env.get("NEO4J_PASSWORD") or ""          # kept exactly as given (may contain spaces)
    database = (env.get("NEO4J_DATABASE") or "").strip() or DEFAULT_DATABASE

    missing = [name for name, value in (("NEO4J_USERNAME", username), ("NEO4J_PASSWORD", password)) if not value]
    if missing:
        raise Neo4jSettingsError(
            "Neo4j is not configured. Set " + " and ".join(missing) + " in the environment before starting Flask.",
            missing)

    invalid_uri = Neo4jSettingsError(
        "NEO4J_URI is not a valid Neo4j address. Use the form neo4j://host:port or bolt://host:port.")
    try:
        parts = urlsplit(uri)
        host, _port = parts.hostname, parts.port       # .port raises ValueError for a bad port
    except ValueError:
        raise invalid_uri from None
    if "@" in parts.netloc:
        raise Neo4jSettingsError(
            "NEO4J_URI must not contain a user name or password. Use NEO4J_USERNAME and NEO4J_PASSWORD.")
    if parts.scheme not in ALLOWED_SCHEMES or not host or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise invalid_uri
    if not DATABASE_NAME_RE.match(database):
        raise Neo4jSettingsError("NEO4J_DATABASE is not a valid Neo4j database name.")
    return Neo4jSettings(uri=uri, username=username, password=password, database=database)


_lock = threading.Lock()
_driver = None
_driver_key = None


def get_driver(settings):
    """Return the shared driver for these settings, creating it on first use (thread-safe).
    Creating a driver does not connect; the first query does."""
    global _driver, _driver_key
    if neo4j is None:
        raise RuntimeError("The Neo4j Python driver is not installed.")
    key = (settings.uri, settings.username, settings.password)
    with _lock:
        if _driver is not None and _driver_key == key:
            return _driver
        old, _driver, _driver_key = _driver, None, None
        if old is not None:
            _close_quietly(old)
        _driver = GraphDatabase.driver(
            settings.uri,
            auth=(settings.username, settings.password),
            connection_timeout=TIMEOUT_SECONDS,
            connection_acquisition_timeout=TIMEOUT_SECONDS,
            max_transaction_retry_time=TIMEOUT_SECONDS,
        )
        _driver_key = key
        return _driver


def close_driver():
    """Close the shared driver (called at exit, and after an authentication failure)."""
    global _driver, _driver_key
    with _lock:
        old, _driver, _driver_key = _driver, None, None
    if old is not None:
        _close_quietly(old)


def _close_quietly(driver):
    try:
        driver.close()
    except Exception as exc:                            # closing must never raise
        _log.warning("Neo4j driver close failed (%s)", type(exc).__name__)


atexit.register(close_driver)


def _neo4j_code(exc):
    """The Neo4j status code of the exception or of any exception it wraps ('' if none)."""
    seen = 0
    while exc is not None and seen < 10:
        if neo4j is not None and isinstance(exc, Neo4jError) and getattr(exc, "code", None):
            return exc.code
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return ""


def _error(state, message, database=None, missing=None):
    body = {"status": "error", "neo4j": state, "message": message}
    if database is not None:
        body["database"] = database
    if missing:
        body["missing"] = list(missing)
    return body


def check_health():
    """Check that Neo4j is reachable and the configured database answers a read query.

    Returns (http_status, body). Never raises. The body never contains the URI,
    user name or password. Runs only RETURN 1 in a read transaction.
    """
    if neo4j is None:
        return 503, _error("driver_missing",
                           "The Neo4j Python driver is not installed. Run: " + DRIVER_INSTALL_HINT)
    try:
        settings = read_settings()
    except Neo4jSettingsError as exc:
        return 503, _error("not_configured", str(exc), missing=exc.missing)

    db = settings.database
    try:
        driver = get_driver(settings)
        records, summary, _keys = driver.execute_query(
            "RETURN 1 AS ok", database_=db, routing_=RoutingControl.READ)
        if len(records) != 1 or records[0]["ok"] != 1:
            _log.warning("Neo4j health check: unexpected answer to RETURN 1")
            return 503, _error("error", "Neo4j answered the health check unexpectedly.", db)
        server = summary.server
        return 200, {
            "status": "success",
            "neo4j": "connected",
            "message": "Neo4j is reachable and the configured database answered a read query.",
            "database": db,
            "serverAgent": server.agent,
            "boltProtocol": ".".join(str(n) for n in server.protocol_version),
            "driverVersion": neo4j.__version__,
        }
    except AuthError as exc:
        close_driver()                                   # do not keep retrying rejected credentials
        _log.warning("Neo4j health check failed: authentication rejected (%s)", type(exc).__name__)
        return 503, _error("auth_failed",
                           "Neo4j rejected the configured user name or password (NEO4J_USERNAME / NEO4J_PASSWORD).", db)
    except ServiceUnavailable as exc:
        _log.warning("Neo4j health check failed: server unreachable (%s)", type(exc).__name__)
        return 503, _error("unreachable",
                           "Neo4j is not reachable. Check that the database is started in Neo4j Desktop.", db)
    except Exception as exc:                             # includes Neo4jError, DriverError, OSError
        code = _neo4j_code(exc)
        if code.endswith("DatabaseNotFound"):
            _log.warning("Neo4j health check failed: database not found (%s)", code)
            return 503, _error("database_not_found",
                               "The database named in NEO4J_DATABASE does not exist on this Neo4j server.", db)
        _log.warning("Neo4j health check failed (%s%s)", type(exc).__name__, (", " + code) if code else "")
        return 503, _error("error", "The Neo4j health check failed. See the Flask terminal for the error type.", db)
