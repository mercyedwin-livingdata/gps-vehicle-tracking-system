-- A trip can have its own driver (the vehicle keeps its "usual" driver in vehicles.driver_name).
-- Additive only; safe to run more than once. NULL means "use the vehicle's usual driver".
USE gps_tracking;

ALTER TABLE trips
  ADD COLUMN IF NOT EXISTS driver_name VARCHAR(100) NULL AFTER vehicle_id;
