-- Experimental, exact-build-only. Make a private SQLite .backup FIRST.
-- Do not apply while anyone is watching. See docs/debloat.md.
-- No package uninstall, firmware setting, executable or support flag is changed.
CREATE TEMP TABLE guard_assert (n INTEGER NOT NULL CHECK (n = 1));
BEGIN IMMEDIATE;
UPDATE package_app_info SET app_disable = 'true'
 WHERE app_id = 'org.tizen.was-appsync'
   AND app_disable = 'false'
   AND app_support_disable = 'false'
   AND app_exec = '/usr/apps/org.tizen.was-appsync/bin/was-appsync'
   AND NOT EXISTS (
       SELECT 1 FROM package_app_info_for_uid
       WHERE app_id = 'org.tizen.was-appsync'
   );
-- With sqlite3 -bail, mismatch aborts before COMMIT; connection close rolls back.
INSERT INTO guard_assert VALUES (changes());
COMMIT;
SELECT app_id, app_disable, app_support_disable, app_exec
 FROM package_app_info WHERE app_id = 'org.tizen.was-appsync';
