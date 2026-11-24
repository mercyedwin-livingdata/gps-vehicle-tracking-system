-- Trip-based scheduling migration for gps_tracking (MariaDB 10.4).
-- Additive only: nothing is dropped. Safe to run more than once.
-- Backup taken before running: gps_tracking_backup_20261022_105215.sql
USE gps_tracking;

-- ── vehicles: new columns (appended after the existing ones) ─────────
ALTER TABLE vehicles
  ADD COLUMN IF NOT EXISTS plate_number VARCHAR(20)  NULL,
  ADD COLUMN IF NOT EXISTS model_name   VARCHAR(100) NULL,
  ADD COLUMN IF NOT EXISTS seat_count   INT          NULL,
  ADD COLUMN IF NOT EXISTS driver_name  VARCHAR(100) NULL;

-- Backfill the 4 existing vehicles from their legacy columns (only fills NULLs).
-- seat_count = passenger seats, not counting the driver: Front + rows of three.
UPDATE vehicles SET plate_number = plate  WHERE plate_number IS NULL AND plate  IS NOT NULL;
UPDATE vehicles SET model_name   = name   WHERE model_name   IS NULL AND name   IS NOT NULL;
UPDATE vehicles SET driver_name  = driver WHERE driver_name  IS NULL AND driver IS NOT NULL;
UPDATE vehicles SET seat_count = CASE model_name
                                   WHEN 'Toyota Sienna'    THEN 7
                                   WHEN 'Toyota Hiace Bus' THEN 13
                                   ELSE 4 END
 WHERE seat_count IS NULL;

ALTER TABLE vehicles ADD UNIQUE INDEX IF NOT EXISTS uq_vehicles_plate_number (plate_number);

-- ── trips: add the scheduling columns to the existing (empty) table ──
-- Legacy columns (start_location, end_location, distance, duration, date) are left untouched.
ALTER TABLE trips
  MODIFY COLUMN vehicle_id INT(11) NOT NULL,
  ADD COLUMN IF NOT EXISTS origin                      VARCHAR(100) NOT NULL,
  ADD COLUMN IF NOT EXISTS destination                 VARCHAR(100) NOT NULL,
  ADD COLUMN IF NOT EXISTS scheduled_departure         DATETIME     NOT NULL,
  ADD COLUMN IF NOT EXISTS scheduled_arrival           DATETIME     NOT NULL,
  ADD COLUMN IF NOT EXISTS estimated_duration_minutes  INT          NOT NULL,
  ADD COLUMN IF NOT EXISTS actual_departure            DATETIME     NULL,
  ADD COLUMN IF NOT EXISTS actual_arrival              DATETIME     NULL,
  ADD COLUMN IF NOT EXISTS delay_minutes               INT          NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS delay_reason                VARCHAR(255) NULL,
  ADD COLUMN IF NOT EXISTS status ENUM('scheduled','moving','stopped','paused','arrived','cancelled')
                                                                    NOT NULL DEFAULT 'scheduled',
  ADD COLUMN IF NOT EXISTS trip_date                   DATE         NOT NULL,
  ADD INDEX IF NOT EXISTS idx_trips_trip_date (trip_date),
  ADD CONSTRAINT fk_trips_vehicle FOREIGN KEY (vehicle_id) REFERENCES vehicles(id);

-- ── passengers ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS passengers (
  id                INT(11)      NOT NULL AUTO_INCREMENT,
  trip_id           INT(11)      NOT NULL,
  passenger_name    VARCHAR(100) NOT NULL,
  seat_label        VARCHAR(30)  NOT NULL,
  drop_off_location VARCHAR(100) NOT NULL,
  alighted_at       DATETIME     NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_passengers_trip_seat (trip_id, seat_label),   -- two passengers can't share a seat
  CONSTRAINT fk_passengers_trip FOREIGN KEY (trip_id) REFERENCES trips(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- ── trip_events ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trip_events (
  id          INT(11)      NOT NULL AUTO_INCREMENT,
  trip_id     INT(11)      NOT NULL,
  event_type  VARCHAR(30)  NOT NULL,
  location    VARCHAR(100) NULL,
  event_time  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  note        VARCHAR(255) NULL,
  PRIMARY KEY (id),
  KEY idx_trip_events_trip (trip_id),
  CONSTRAINT fk_trip_events_trip FOREIGN KEY (trip_id) REFERENCES trips(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
