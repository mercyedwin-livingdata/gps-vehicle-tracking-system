-- Drivers become independent of vehicles: a driver is chosen per trip.
-- The app runs all of this by itself at startup (each step checks first, so it is safe to run again).
-- Running it by hand is optional. Nothing is dropped: vehicles.driver, vehicles.driver_name and vehicles.driver_phone
-- stay in place (unused by the pages), and trips.driver_name keeps each old trip's stored name.
USE gps_tracking;

-- 1. the table
CREATE TABLE IF NOT EXISTS drivers (
  id INT AUTO_INCREMENT PRIMARY KEY,
  full_name VARCHAR(100) NOT NULL,
  phone VARCHAR(20) NULL,
  status ENUM('active','inactive') NOT NULL DEFAULT 'active',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (full_name, phone)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- remembers that the one-time migration below has run (so a driver you rename later is never re-created from old names)
CREATE TABLE IF NOT EXISTS app_migrations (
  name VARCHAR(60) PRIMARY KEY,
  applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2. each trip points at its driver
ALTER TABLE trips ADD COLUMN driver_id INT NULL AFTER driver_name;
ALTER TABLE trips ADD CONSTRAINT fk_trips_driver FOREIGN KEY (driver_id) REFERENCES drivers (id) ON DELETE RESTRICT;

-- 3. one-time migration: one drivers row per distinct non-empty name on vehicles (phone copied from vehicles.driver_phone), then on trips
INSERT IGNORE INTO drivers (full_name, phone)
SELECT n, MAX(p) FROM (
  SELECT TRIM(driver_name) AS n, driver_phone AS p FROM vehicles WHERE driver_name IS NOT NULL AND TRIM(driver_name) <> ''
  UNION ALL
  SELECT TRIM(driver), NULL FROM vehicles WHERE driver IS NOT NULL AND TRIM(driver) <> ''
) x GROUP BY n;

INSERT IGNORE INTO drivers (full_name)
SELECT DISTINCT TRIM(t.driver_name) FROM trips t
WHERE t.driver_name IS NOT NULL AND TRIM(t.driver_name) <> ''
  AND NOT EXISTS (SELECT 1 FROM drivers d WHERE d.full_name = TRIM(t.driver_name));

-- link every trip by its stored name (or, if the trip has none, its vehicle's old driver)
UPDATE trips t JOIN vehicles v ON v.id = t.vehicle_id
JOIN drivers d ON d.full_name = TRIM(COALESCE(NULLIF(TRIM(t.driver_name), ''), v.driver_name, v.driver))
SET t.driver_id = d.id WHERE t.driver_id IS NULL;

INSERT INTO app_migrations (name) VALUES ('drivers_v1');

-- ROLLBACK (deletes every driver you added and the driver links; trips.driver_name and the vehicles columns are untouched):
-- ALTER TABLE trips DROP FOREIGN KEY fk_trips_driver;
-- ALTER TABLE trips DROP COLUMN driver_id;
-- DROP TABLE drivers;
-- DROP TABLE app_migrations;
-- Note: if you roll back but keep this version of the app, it re-creates the table at the next start. Roll back the app files too.
