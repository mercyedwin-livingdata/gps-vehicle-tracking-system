-- Records WHERE a passenger got off (the report shows "got off at <place> <time>").
-- Additive only; safe to run more than once.
USE gps_tracking;

ALTER TABLE passengers
  ADD COLUMN IF NOT EXISTS alighted_location VARCHAR(100) NULL AFTER alighted_at;
