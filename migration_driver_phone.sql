-- Adds the usual driver's phone number to each vehicle (optional; existing rows keep NULL).
-- The app also does this by itself at startup if the column is missing, so running this by hand is optional.
USE gps_tracking;

ALTER TABLE vehicles ADD COLUMN driver_phone VARCHAR(20) NULL;

-- Rollback (this deletes the saved phone numbers):
-- ALTER TABLE vehicles DROP COLUMN driver_phone;
