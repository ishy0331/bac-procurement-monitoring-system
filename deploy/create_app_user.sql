-- deploy/create_app_user.sql
-- Creates the dedicated MariaDB account the app uses instead of root.
--
-- 1. Replace BOTH occurrences of CHANGE-ME-strong-password with one strong
--    password (e.g. from:  python -c "import secrets; print(secrets.token_urlsafe(24))").
-- 2. Run this whole file in HeidiSQL while connected as root.
-- 3. Put the same user name and password in DB_CONFIG in backend/config.py.
--
-- The app only needs to read and change rows. It never creates, alters or
-- drops tables, so it gets SELECT, INSERT, UPDATE, DELETE on this one database.
-- (Run seed.py and schema changes as root, not as this user.)
-- Do not save this file with the real password in it.

CREATE USER IF NOT EXISTS 'bac_app'@'localhost' IDENTIFIED BY 'CHANGE-ME-strong-password';
CREATE USER IF NOT EXISTS 'bac_app'@'127.0.0.1' IDENTIFIED BY 'CHANGE-ME-strong-password';

GRANT SELECT, INSERT, UPDATE, DELETE
    ON procurement_db_frontend_test.* TO 'bac_app'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE
    ON procurement_db_frontend_test.* TO 'bac_app'@'127.0.0.1';

-- Check:
SHOW GRANTS FOR 'bac_app'@'localhost';
SHOW GRANTS FOR 'bac_app'@'127.0.0.1';
