"""
backend/migrate_documents.py - ONE-TIME schema addition for document management.

Creates the table procurement_documents (if it does not exist yet). Run it once, from the project folder:

    python backend\\migrate_documents.py

It asks for a MariaDB account that may create tables (normally root). The app user bac_app cannot
create tables, and that is intended. The password is typed at the prompt and never stored or printed.
Host, port and database come from DB_CONFIG in backend/config.py.

Safe to run again: if the table already exists, nothing is changed.
The foreign-key column types are copied from procurements.procurement_id and users.user_id, so the
foreign keys match the existing schema exactly. Existing tables and rows are not touched.
"""

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mysql.connector                     # noqa: E402

from config import DB_CONFIG               # noqa: E402

EXPECTED_DATABASE = "procurement_db_frontend_test"

DDL = """
CREATE TABLE IF NOT EXISTS procurement_documents (
    document_id     INT UNSIGNED NOT NULL AUTO_INCREMENT,
    procurement_id  {proc_type} NOT NULL,
    doc_type        VARCHAR(60)  NOT NULL,
    title           VARCHAR(255) NOT NULL,
    original_name   VARCHAR(255) NOT NULL,
    stored_name     VARCHAR(80)  NOT NULL,
    mime_type       VARCHAR(120) NOT NULL,
    size_bytes      INT UNSIGNED NOT NULL,
    sha256          CHAR(64)     NOT NULL,
    is_public       TINYINT(1)   NOT NULL DEFAULT 0,
    uploaded_by     {user_type} NULL,
    uploaded_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at      DATETIME     NULL,
    deleted_by      {user_type} NULL,
    PRIMARY KEY (document_id),
    UNIQUE KEY uq_documents_stored_name (stored_name),
    KEY idx_documents_procurement (procurement_id, deleted_at),
    KEY idx_documents_public (is_public, deleted_at),
    CONSTRAINT fk_documents_procurement FOREIGN KEY (procurement_id)
        REFERENCES procurements (procurement_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT fk_documents_uploaded_by FOREIGN KEY (uploaded_by)
        REFERENCES users (user_id) ON DELETE SET NULL ON UPDATE CASCADE,
    CONSTRAINT fk_documents_deleted_by FOREIGN KEY (deleted_by)
        REFERENCES users (user_id) ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Uploaded procurement documents (files are stored in UPLOAD_DIR, not in the database)'
"""


def column_type(cur, table, column):
    cur.execute("SELECT COLUMN_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = %s AND COLUMN_NAME = %s", (table, column))
    row = cur.fetchone()
    if row is None:
        sys.exit(f"Stopped: {table}.{column} was not found. Is this the right database?")
    value = row[0].decode() if isinstance(row[0], (bytes, bytearray)) else str(row[0])
    return value.upper()


def main():
    print("Document management: create table procurement_documents\n")
    print(f"Database: {DB_CONFIG.get('database')} on {DB_CONFIG.get('host')}:{DB_CONFIG.get('port')}")
    user = input("MariaDB account that may create tables [root]: ").strip() or "root"
    password = getpass.getpass(f"Password for {user}: ")
    settings = dict(DB_CONFIG, user=user, password=password)
    try:
        conn = mysql.connector.connect(**settings)
    except Exception as exc:
        sys.exit(f"Could not connect: {type(exc).__name__}. Check the account and password.")
    try:
        cur = conn.cursor()
        cur.execute("SELECT DATABASE()")
        database = cur.fetchone()[0]
        if database != EXPECTED_DATABASE:
            sys.exit(f"Stopped: connected to '{database}', expected '{EXPECTED_DATABASE}'. Nothing was changed.")
        cur.execute("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() "
                    "AND TABLE_NAME = 'procurement_documents'")
        if cur.fetchone()[0]:
            print("The table procurement_documents already exists. Nothing was changed.")
            return 0
        ddl = DDL.format(proc_type=column_type(cur, "procurements", "procurement_id"),
                         user_type=column_type(cur, "users", "user_id"))
        cur.execute(ddl)
        cur.execute("SHOW COLUMNS FROM procurement_documents")
        print(f"Created procurement_documents ({len(cur.fetchall())} columns).")
        print("The app user bac_app can use it at once (it has SELECT/INSERT/UPDATE/DELETE on this database).")
        print("Restart the server, then open a procurement in the registry and choose Documents.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
