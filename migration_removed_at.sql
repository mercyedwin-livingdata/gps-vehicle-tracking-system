-- "Remove" for drivers and vehicles. A removed row is hidden everywhere but kept for old trips and reports.
-- The app adds both columns by itself at startup if they are missing (safe to run again); running this by hand is optional.
USE gps_tracking;

ALTER TABLE vehicles ADD COLUMN removed_at DATETIME NULL;
ALTER TABLE drivers  ADD COLUMN removed_at DATETIME NULL;

-- Rollback (this forgets which rows were removed: they would all show up again as active):
-- ALTER TABLE vehicles DROP COLUMN removed_at;
-- ALTER TABLE drivers  DROP COLUMN removed_at;
