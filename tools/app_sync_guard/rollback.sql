-- ONLY rollback of this runbook's original false -> true guard change.
-- Verify the saved original row first; never restore an entire live DB for this.
CREATE TEMP TABLE guard_assert (n INTEGER NOT NULL CHECK (n = 1));
BEGIN IMMEDIATE;
UPDATE package_app_info SET app_disable = 'false'
 WHERE app_id = 'org.tizen.was-appsync'
   AND app_disable = 'true'
   AND app_support_disable = 'false'
   AND app_exec = '/usr/apps/org.tizen.was-appsync/bin/was-appsync'
   AND NOT EXISTS (
       SELECT 1 FROM package_app_info_for_uid
       WHERE app_id = 'org.tizen.was-appsync'
   );
INSERT INTO guard_assert VALUES (changes());
COMMIT;
SELECT app_id, app_disable, app_support_disable, app_exec
 FROM package_app_info WHERE app_id = 'org.tizen.was-appsync';
