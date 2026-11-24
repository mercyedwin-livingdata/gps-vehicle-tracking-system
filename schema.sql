-- Schema only. No INSERT statements and no account credentials.

CREATE TABLE `locations` (
  `id` int(11) NOT NULL,
  `vehicle_id` int(11) DEFAULT NULL,
  `latitude` float DEFAULT NULL,
  `longitude` float DEFAULT NULL,
  `speed` float DEFAULT NULL,
  `timestamp` datetime DEFAULT current_timestamp()
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE `passengers` (
  `id` int(11) NOT NULL,
  `trip_id` int(11) NOT NULL,
  `passenger_name` varchar(100) NOT NULL,
  `seat_label` varchar(30) NOT NULL,
  `drop_off_location` varchar(100) NOT NULL,
  `alighted_at` datetime DEFAULT NULL,
  `alighted_location` varchar(100) DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE `trips` (
  `id` int(11) NOT NULL,
  `vehicle_id` int(11) NOT NULL,
  `driver_name` varchar(100) DEFAULT NULL,
  `driver_id` int(11) DEFAULT NULL,
  `start_location` varchar(200) DEFAULT NULL,
  `end_location` varchar(200) DEFAULT NULL,
  `distance` float DEFAULT NULL,
  `duration` int(11) DEFAULT NULL,
  `date` date DEFAULT NULL,
  `origin` varchar(100) NOT NULL,
  `destination` varchar(100) NOT NULL,
  `scheduled_departure` datetime NOT NULL,
  `scheduled_arrival` datetime NOT NULL,
  `estimated_duration_minutes` int(11) NOT NULL,
  `actual_departure` datetime DEFAULT NULL,
  `actual_arrival` datetime DEFAULT NULL,
  `delay_minutes` int(11) NOT NULL DEFAULT 0,
  `delay_reason` varchar(255) DEFAULT NULL,
  `status` enum('scheduled','moving','stopped','paused','arrived','cancelled') NOT NULL DEFAULT 'scheduled',
  `trip_date` date NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE `trip_events` (
  `id` int(11) NOT NULL,
  `trip_id` int(11) NOT NULL,
  `event_type` varchar(30) NOT NULL,
  `location` varchar(100) DEFAULT NULL,
  `event_time` datetime NOT NULL DEFAULT current_timestamp(),
  `note` varchar(255) DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE `users` (
  `id` int(11) NOT NULL,
  `full_name` varchar(100) NOT NULL,
  `email` varchar(120) NOT NULL,
  `password_hash` varchar(255) NOT NULL,
  `role` enum('admin','viewer') NOT NULL DEFAULT 'viewer',
  `status` enum('pending','approved','rejected') NOT NULL DEFAULT 'pending',
  `created_at` datetime DEFAULT current_timestamp(),
  `approved_at` datetime DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE `vehicles` (
  `id` int(11) NOT NULL,
  `name` varchar(100) DEFAULT NULL,
  `driver` varchar(100) DEFAULT NULL,
  `plate` varchar(20) DEFAULT NULL,
  `status` varchar(20) DEFAULT NULL,
  `speed` float DEFAULT NULL,
  `fuel` float DEFAULT NULL,
  `latitude` float DEFAULT NULL,
  `longitude` float DEFAULT NULL,
  `route` varchar(200) DEFAULT NULL,
  `eta` varchar(50) DEFAULT NULL,
  `trips` int(11) DEFAULT NULL,
  `plate_number` varchar(20) DEFAULT NULL,
  `model_name` varchar(100) DEFAULT NULL,
  `seat_count` int(11) DEFAULT NULL,
  `driver_name` varchar(100) DEFAULT NULL,
  `driver_phone` varchar(20) DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS drivers (
  id INT AUTO_INCREMENT PRIMARY KEY,
  full_name VARCHAR(100) NOT NULL,
  phone VARCHAR(20) NULL,
  status ENUM('active','inactive') NOT NULL DEFAULT 'active',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (full_name, phone)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS app_migrations (
  name VARCHAR(60) PRIMARY KEY,
  applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
