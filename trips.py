"""Trip scheduling API: trips, passengers, seat layout, events, report and KPIs.

Registered from app.py with register_trip_routes(app, mysql).
Every query is parameterised; status changes are single guarded UPDATEs
(WHERE status IN (...)) so two simultaneous requests can't both succeed.
mark_arrived() is shared by POST /api/trips/<id>/arrive and the simulator.
"""
import os
import re
import smtplib
import ssl
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, date
from decimal import Decimal
from email.message import EmailMessage
from email.utils import formataddr
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

import MySQLdb
import MySQLdb.cursors
from dotenv import dotenv_values
from flask import Response, jsonify, request
from auth import admin_required, is_viewer, login_required, strip_for_viewer
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import config

MAX_SEATS = 60
ON_TIME_GRACE_MINUTES = 0   # an arrived trip is "on time" when delay_minutes <= this
SEAT_POSITIONS = ('Left', 'Middle', 'Right')
DUP_KEY = 1062
PLATE_RE = re.compile(r'[A-Za-z0-9-]{3,15}')
IN_PROGRESS = ('moving', 'paused', 'stopped')   # vehicle is on the road
TOWN_BY_KEY = {name.lower(): name for name in config.TOWNS}


# ── SEAT LAYOUT ──────────────────────────────────
def seat_labels(seat_count):
    """Front seat beside the driver, then rows of three starting at Row 2.
    7 seats -> Front, Row 2 - Left/Middle/Right, Row 3 - Left/Middle/Right.
    The last row is partial when seat_count - 1 isn't a multiple of 3."""
    labels = ['Front']
    for i in range(seat_count - 1):
        labels.append(f"Row {i // 3 + 2} - {SEAT_POSITIONS[i % 3]}")
    return labels[:seat_count]


def _seat_key(label):
    return ' '.join(str(label).lower().replace('–', '-').split())


# ── SMALL HELPERS ────────────────────────────────
def _error(message, code=400, **extra):
    return jsonify({'error': message, **extra}), code


def _text(value, max_len):
    """Trimmed string, or '' if missing / not a string."""
    return value.strip()[:max_len] if isinstance(value, str) else ''


def _driver_name(value, errors, key='full_name'):
    """Trimmed driver name; adds an error (and returns '') when it is missing or longer than 100 characters."""
    name = value.strip() if isinstance(value, str) else ''
    if not name:
        errors.append(f'{key} is required' if key == 'full_name' else f"{key} can't be empty")
    elif len(name) > 100:
        errors.append(f'{key} is too long (100 characters at most)')
        return ''
    return name


def _town(value):
    """Canonical town name from config.TOWNS (case-insensitive), or None."""
    return TOWN_BY_KEY.get(value.strip().lower()) if isinstance(value, str) else None


def _parse_datetime(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required (YYYY-MM-DD HH:MM)")
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(f"{field} must look like YYYY-MM-DD HH:MM")
    if dt.tzinfo is not None:
        raise ValueError(f"{field} must not include a timezone")
    return dt.replace(microsecond=0)


def parse_date_arg(value):
    """?date=YYYY-MM-DD -> date. Missing means today (server date)."""
    if not value:
        return date.today()
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        raise ValueError("date must look like YYYY-MM-DD")


def _clean(value):
    """Make DB values JSON friendly (datetimes -> ISO strings, Decimal -> number)."""
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _row(row):
    return {k: _clean(v) for k, v in row.items()} if row else None


def _num(value, ndigits=None):
    if value is None:
        return None
    value = float(value)
    return round(value, ndigits) if ndigits is not None else value


def late_minutes(status, scheduled_arrival, delay_minutes, now):
    """Whole minutes late. Live for trips on the road (measured against the scheduled
    arrival right now); the stored final figure once arrived; 0 otherwise."""
    if status in IN_PROGRESS:
        return max(0, int((now - scheduled_arrival).total_seconds() // 60))
    if status == 'arrived':
        return delay_minutes
    return 0


TRIP_SELECT = """
    SELECT t.id, t.vehicle_id, v.plate_number, v.model_name, COALESCE(t.driver_name, v.driver_name) AS driver_name, t.driver_id, v.seat_count,
           t.origin, t.destination, t.scheduled_departure, t.scheduled_arrival,
           t.estimated_duration_minutes, t.actual_departure, t.actual_arrival,
           t.delay_minutes, t.delay_reason, t.status, t.trip_date
    FROM trips t JOIN vehicles v ON v.id = t.vehicle_id
"""


def _load_trips(cur, where, params, with_events=False):
    cur.execute(TRIP_SELECT + where + " ORDER BY t.scheduled_departure, t.id", params)
    now = datetime.now()
    trips = []
    for r in cur.fetchall():
        late = late_minutes(r['status'], r['scheduled_arrival'], r['delay_minutes'], now)
        t = _row(r)
        t['late_minutes'] = late          # live while on the road, final once arrived
        t['delayed'] = late > 0
        trips.append(t)
    if not trips:
        return trips

    ids = [t['id'] for t in trips]
    marks = ','.join(['%s'] * len(ids))
    cur.execute(f"""
        SELECT id, trip_id, passenger_name, seat_label, drop_off_location, alighted_at, alighted_location
        FROM passengers WHERE trip_id IN ({marks}) ORDER BY id
    """, ids)
    by_trip = {i: [] for i in ids}
    for p in cur.fetchall():
        by_trip[p['trip_id']].append(_row(p))

    events = {i: [] for i in ids}
    if with_events:
        cur.execute(f"""
            SELECT id, trip_id, event_type, location, event_time, note
            FROM trip_events WHERE trip_id IN ({marks}) ORDER BY event_time, id
        """, ids)
        for e in cur.fetchall():
            events[e['trip_id']].append(_row(e))

    for t in trips:
        plist = by_trip[t['id']]
        t['passengers'] = plist
        t['passengers_total'] = len(plist)
        t['passengers_alighted'] = sum(1 for p in plist if p['alighted_at'])
        t['passengers_onboard'] = t['passengers_total'] - t['passengers_alighted']
        if with_events:
            t['events'] = events[t['id']]
    return trips


LIVE_LATE_SQL = ("(delay_minutes > 0 OR (status IN ('moving','paused','stopped') "
                 "AND TIMESTAMPDIFF(MINUTE, scheduled_arrival, %s) > 0))")


def trip_status_summary(cur, day):
    """Counts for ONE date. 'total' is trips; 'vehicles_total' is distinct vehicles with a
    non-cancelled trip. 'idle' and 'delayed' keep the dashboard's old keys:
    idle = scheduled + paused + stopped, delayed = late now or arrived late."""
    cur.execute("SELECT status, COUNT(*) AS n FROM trips WHERE trip_date = %s GROUP BY status", (day,))
    statuses = ('scheduled', 'moving', 'paused', 'stopped', 'arrived', 'cancelled')
    summary = {s: 0 for s in statuses}
    for r in cur.fetchall():
        summary[r['status']] = r['n']
    cur.execute(f"SELECT COUNT(*) AS n FROM trips WHERE trip_date = %s AND {LIVE_LATE_SQL}",
                (day, datetime.now()))
    summary['delayed'] = cur.fetchone()['n']
    cur.execute("""SELECT COUNT(DISTINCT vehicle_id) AS n FROM trips
                   WHERE trip_date = %s AND status <> 'cancelled'""", (day,))
    summary['vehicles_total'] = cur.fetchone()['n']
    summary['idle'] = summary['scheduled'] + summary['paused'] + summary['stopped']
    summary['total'] = sum(summary[s] for s in statuses)
    summary['date'] = day.isoformat()
    return summary


def active_vehicles(cur, day):
    """Vehicles on the road: those with a moving / paused / stopped trip on `day`.
    Vehicles without one are not returned."""
    cur.execute("""
        SELECT v.id, COALESCE(v.model_name, v.name) AS name,
               COALESCE(t.driver_name, v.driver_name, v.driver) AS driver, COALESCE(v.plate_number, v.plate) AS plate,
               v.speed, v.fuel, v.latitude, v.longitude, v.route, v.eta, v.seat_count,
               t.id AS trip_id, t.status AS trip_status, t.origin, t.destination,
               t.scheduled_arrival, t.delay_minutes,
               (SELECT COUNT(*) FROM trips x WHERE x.vehicle_id = v.id AND x.trip_date = %s
                  AND x.status <> 'cancelled') AS trips
        FROM trips t JOIN vehicles v ON v.id = t.vehicle_id
        WHERE t.trip_date = %s AND t.status IN ('moving','paused','stopped') AND v.removed_at IS NULL
        ORDER BY t.scheduled_departure, t.id
    """, (day, day))
    now = datetime.now()
    out = []
    for r in cur.fetchall():
        late = late_minutes(r['trip_status'], r['scheduled_arrival'], r['delay_minutes'], now)
        row = _row(r)
        # display status: moving / delayed (moving or stopped but late) / idle (paused or stopped)
        if late > 0:
            row['status'] = 'delayed'
        else:
            row['status'] = 'moving' if r['trip_status'] == 'moving' else 'idle'
        row['late_minutes'] = late
        row['delayed'] = late > 0
        out.append(row)
    return out


def set_vehicle_state(cur, vehicle_id, status, speed=0, lat=None, lng=None, route=None, eta=None):
    """Keep the vehicles row (what the map reads) in step with its trip."""
    cur.execute("""UPDATE vehicles SET status = %s, speed = %s,
                       latitude = COALESCE(%s, latitude), longitude = COALESCE(%s, longitude),
                       route = COALESCE(%s, route), eta = COALESCE(%s, eta)
                   WHERE id = %s""", (status, speed, lat, lng, route, eta, vehicle_id))


def mark_arrived(cur, trip_id, vehicle_id, destination, now, delay_reason=None, location=None, note=None,
                 fallback_reason=None):
    """The one place a trip becomes 'arrived' (used by POST .../arrive and the simulator).
    Guarded UPDATE: only moving/paused/stopped trips change. delay_minutes is the whole minutes
    between scheduled_arrival and now (0 if early). delay_reason: an explicit one wins, then one
    already stored, then fallback_reason - and the fallback is used only when the trip is actually late.
    Returns False if the trip was not arrivable."""
    cur.execute("""
        UPDATE trips SET status = 'arrived', actual_arrival = %s,
               delay_minutes = GREATEST(0, TIMESTAMPDIFF(MINUTE, scheduled_arrival, %s)),
               delay_reason = COALESCE(%s, delay_reason,
                                       IF(TIMESTAMPDIFF(MINUTE, scheduled_arrival, %s) > 0, %s, NULL))
        WHERE id = %s AND status IN ('moving','paused','stopped')
    """, (now, now, delay_reason, now, fallback_reason, trip_id))
    if cur.rowcount == 0:
        return False
    dest = config.TOWNS.get(destination)
    set_vehicle_state(cur, vehicle_id, 'idle', 0, dest['lat'] if dest else None,
                      dest['lng'] if dest else None, eta='Arrived')
    cur.execute("""INSERT INTO trip_events (trip_id, event_type, location, event_time, note)
                   VALUES (%s,'arrive',%s,%s,%s)""", (trip_id, location or destination, now, note))
    return True


# ── FLEET (registered vehicles; the plate number is the unique identifier) ──────────────
def fleet_rows(cur):
    """Every registered vehicle, with or without a trip, sorted by vehicle name (then plate)."""
    cur.execute("""SELECT plate_number, model_name, seat_count FROM vehicles
                   WHERE plate_number IS NOT NULL AND removed_at IS NULL ORDER BY model_name, plate_number""")
    return cur.fetchall()


# ── driver phone (optional): digits, +, spaces and dashes only; 7 to 15 digits (Nigerian numbers, e.g. +234 803 123 4567) ──
PHONE_CHARS_RE = re.compile(r'[0-9+\-\s]+')
_phone_column = {'ready': False}


def ensure_vehicle_columns(cur):
    """Adds vehicles.driver_phone if it is missing. Safe to run again and again (it checks first)."""
    if _phone_column['ready']:
        return
    cur.execute("""SELECT COUNT(*) AS n FROM information_schema.COLUMNS
                   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'vehicles' AND COLUMN_NAME = 'driver_phone'""")
    row = cur.fetchone()
    if not (row['n'] if isinstance(row, dict) else row[0]):
        cur.execute("ALTER TABLE vehicles ADD COLUMN driver_phone VARCHAR(20) NULL")
    _phone_column['ready'] = True


def parse_phone(value, label='phone'):
    """(phone or None, error or None). Empty means "no phone"."""
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, f'{label} must be text'
    phone = ' '.join(value.split())
    if not phone:
        return None, None
    digits = sum(c.isdigit() for c in phone)
    if not PHONE_CHARS_RE.fullmatch(phone) or '+' in phone[1:]:
        return None, f'{label} can only contain digits, spaces, dashes and a leading +'
    if not 7 <= digits <= 15:
        return None, f'{label} must have 7 to 15 digits, e.g. +234 803 123 4567'
    if len(phone) > 20:
        return None, f'{label} is too long (20 characters at most, including spaces and dashes)'
    return phone, None


# ── DRIVERS (independent of vehicles: a driver is chosen per trip) ──────────────────────────
_driver_schema = {'ready': False}
_driver_schema_lock = threading.Lock()
DRIVER_OPEN_STATUSES = ('scheduled', 'moving', 'paused', 'stopped')      # a driver is "busy" on these trips


def _has_column(cur, table, column):
    cur.execute("""SELECT COUNT(*) AS n FROM information_schema.COLUMNS
                   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s""", (table, column))
    return bool(cur.fetchone()['n'])


def ensure_driver_schema(conn):
    """Creates `drivers`, adds trips.driver_id (+ foreign key) and runs the one-time data migration.
    Safe to run on every start: each step checks first, and the migration is recorded in `app_migrations`
    so it never runs twice (a driver you rename or add later is never re-created from old names)."""
    if _driver_schema['ready']:
        return
    with _driver_schema_lock:
        if _driver_schema['ready']:
            return
        cur = conn.cursor(MySQLdb.cursors.DictCursor)
        try:
            cur.execute("""CREATE TABLE IF NOT EXISTS drivers (
                               id INT AUTO_INCREMENT PRIMARY KEY,
                               full_name VARCHAR(100) NOT NULL,
                               phone VARCHAR(20) NULL,
                               status ENUM('active','inactive') NOT NULL DEFAULT 'active',
                               created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                               UNIQUE (full_name, phone)
                           ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            cur.execute("""CREATE TABLE IF NOT EXISTS app_migrations (
                               name VARCHAR(60) PRIMARY KEY,
                               applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
                           ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            for table in ('vehicles', 'drivers'):                   # "Remove": kept for old reports, hidden everywhere else
                if not _has_column(cur, table, 'removed_at'):
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN removed_at DATETIME NULL")
            if not _has_column(cur, 'trips', 'driver_id'):
                cur.execute("ALTER TABLE trips ADD COLUMN driver_id INT NULL AFTER driver_name")
            cur.execute("""SELECT COUNT(*) AS n FROM information_schema.TABLE_CONSTRAINTS
                           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'trips'
                             AND CONSTRAINT_NAME = 'fk_trips_driver' AND CONSTRAINT_TYPE = 'FOREIGN KEY'""")
            if not cur.fetchone()['n']:
                cur.execute("""ALTER TABLE trips ADD CONSTRAINT fk_trips_driver
                               FOREIGN KEY (driver_id) REFERENCES drivers (id) ON DELETE RESTRICT""")
            conn.commit()

            cur.execute("SELECT 1 FROM app_migrations WHERE name = 'drivers_v1' FOR UPDATE")
            if not cur.fetchone():
                # one drivers row per distinct non-empty name on vehicles (phone copied from vehicles.driver_phone), then on trips
                cur.execute("""INSERT IGNORE INTO drivers (full_name, phone)
                               SELECT n, MAX(p) FROM (
                                   SELECT TRIM(driver_name) AS n, driver_phone AS p FROM vehicles
                                    WHERE driver_name IS NOT NULL AND TRIM(driver_name) <> ''
                                   UNION ALL
                                   SELECT TRIM(driver), NULL FROM vehicles WHERE driver IS NOT NULL AND TRIM(driver) <> ''
                               ) x GROUP BY n""")
                cur.execute("""INSERT IGNORE INTO drivers (full_name)
                               SELECT DISTINCT TRIM(t.driver_name) FROM trips t
                                WHERE t.driver_name IS NOT NULL AND TRIM(t.driver_name) <> ''
                                  AND NOT EXISTS (SELECT 1 FROM drivers d WHERE d.full_name = TRIM(t.driver_name))""")
                # link each trip by its stored name (or, when the trip has none, the name of its vehicle's old driver)
                cur.execute("""UPDATE trips t JOIN vehicles v ON v.id = t.vehicle_id
                               JOIN drivers d ON d.full_name = TRIM(COALESCE(NULLIF(TRIM(t.driver_name), ''), v.driver_name, v.driver))
                               SET t.driver_id = d.id WHERE t.driver_id IS NULL""")
                cur.execute("INSERT INTO app_migrations (name) VALUES ('drivers_v1')")
                conn.commit()
            _driver_schema['ready'] = True
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


def removed_plate_message(plate, model):
    return (f"Plate {plate} belongs to {model}, which was removed. Restore it on the Vehicles page "
            f"(Show removed, then Restore) instead of registering it again.")


def driver_clash(cur, driver_id, departure, arrival, exclude_trip_id=None):
    """Another scheduled/moving/paused/stopped trip of this driver whose window overlaps [departure, arrival)."""
    marks = ','.join(['%s'] * len(DRIVER_OPEN_STATUSES))
    cur.execute(f"""SELECT id, origin, destination, scheduled_departure, scheduled_arrival, status FROM trips
                    WHERE driver_id = %s AND id <> %s AND status IN ({marks})
                      AND scheduled_departure < %s AND scheduled_arrival > %s
                    ORDER BY scheduled_departure LIMIT 1 FOR UPDATE""",
                (driver_id, exclude_trip_id or 0, *DRIVER_OPEN_STATUSES, arrival, departure))
    return cur.fetchone()


def driver_clash_message(name, clash, departure, arrival):
    return (f"Driver {name} already has trip #{clash['id']} ({clash['origin']} to {clash['destination']}, "
            f"{clash['status']}) from {clash['scheduled_departure']:%Y-%m-%d %H:%M} to {clash['scheduled_arrival']:%H:%M}, "
            f"which overlaps the requested {departure:%Y-%m-%d %H:%M} to {arrival:%H:%M}. Choose another driver or another time.")


def vehicle_input(data):
    """Validate {plate_number, model_name, seat_count}. Driver fields, if sent, are ignored. Returns (values, errors)."""
    errors = []
    plate_raw = _text(data.get('plate_number'), 100)
    if not plate_raw:
        errors.append('plate_number is required')
    elif not PLATE_RE.fullmatch(plate_raw):
        errors.append('plate_number must be 3 to 15 characters: letters, digits and hyphens only')
    model = _text(data.get('model_name'), 100)
    seats = data.get('seat_count')
    if not model:
        errors.append('model_name is required')
    if isinstance(seats, bool) or not isinstance(seats, int) or not 1 <= seats <= MAX_SEATS:
        errors.append(f'seat_count must be a whole number from 1 to {MAX_SEATS}')
    return {'plate_number': plate_raw.upper(), 'model_name': model, 'seat_count': seats}, errors


def insert_vehicle(cur, v, lat=None, lng=None):
    """Add one vehicle row (new vehicles start idle, full tank, at the map centre until a trip places them)."""
    lat = config.MAP_CENTER['lat'] if lat is None else lat
    lng = config.MAP_CENTER['lng'] if lng is None else lng
    cur.execute("""
        INSERT INTO vehicles
        (plate_number, model_name, seat_count,
         name, plate, status, speed, fuel, latitude, longitude, trips)
        VALUES (%s,%s,%s, %s,%s,'idle',0,100,%s,%s,0)
    """, (v['plate_number'], v['model_name'], v['seat_count'],
          v['model_name'], v['plate_number'], lat, lng))
    return cur.lastrowid


def registered_mismatch(plate, vehicle, data):
    """None when the request agrees with the registered vehicle (or doesn't mention name/seats);
    otherwise a message naming the registered values. The plate decides which vehicle it is."""
    model, seats = vehicle['model_name'], vehicle['seat_count']
    sent_model, sent_seats = data.get('model_name'), data.get('seat_count')
    wrong = []
    if isinstance(sent_model, str) and sent_model.strip() \
            and sent_model.strip().casefold() != (model or '').strip().casefold():
        wrong.append(f'the vehicle name "{sent_model.strip()}"')
    if sent_seats not in (None, '') and str(sent_seats).strip() != str(seats):
        wrong.append(f'{sent_seats} seats')
    if not wrong:
        return None
    return (f"Plate {plate} is registered as {model} with {seats} seats, but this request says "
            f"{' and '.join(wrong)}. Use the registered values.")


def begin_trip(cur, trip_id, vehicle_id, origin, destination, now, note=None, location=None):
    """The one place a trip starts moving (POST .../start and the simulator's auto-start).
    Locks the vehicle, refuses if it already has another trip on the road, then a guarded UPDATE
    (only 'scheduled' trips change) puts the vehicle at point A and writes the 'start' event.
    Returns ('started', None) | ('busy', other_trip_id) | ('not_scheduled', None)."""
    cur.execute("SELECT id FROM vehicles WHERE id = %s FOR UPDATE", (vehicle_id,))
    cur.fetchall()
    cur.execute("""SELECT id FROM trips WHERE vehicle_id = %s AND id <> %s
                   AND status IN ('moving','paused','stopped') LIMIT 1 FOR UPDATE""", (vehicle_id, trip_id))
    other = cur.fetchone()
    if other:
        return 'busy', other['id']
    cur.execute("UPDATE trips SET status = 'moving', actual_departure = %s WHERE id = %s AND status = 'scheduled'",
                (now, trip_id))
    if cur.rowcount == 0:
        return 'not_scheduled', None
    o = config.TOWNS.get(origin)
    set_vehicle_state(cur, vehicle_id, 'moving', 0, o['lat'] if o else None, o['lng'] if o else None,
                      f"{origin} → {destination}", '—')
    cur.execute("""INSERT INTO trip_events (trip_id, event_type, location, event_time, note)
                   VALUES (%s,'start',%s,%s,%s)""", (trip_id, location, now, note))
    return 'started', None


def alight_passenger(cur, trip_id, passenger_id, location, now, note=None):
    """The one place a passenger gets off (POST .../alight and the simulator's drop-off stops).
    Guarded UPDATE: only a passenger of this trip who hasn't alighted yet. Writes the 'alight' event
    (location, passenger name and seat). Returns {'passenger_name','seat_label'} or None if nothing changed."""
    cur.execute("""UPDATE passengers SET alighted_at = %s, alighted_location = %s
                   WHERE id = %s AND trip_id = %s AND alighted_at IS NULL""",
                (now, location, passenger_id, trip_id))
    if cur.rowcount == 0:
        return None
    cur.execute("SELECT passenger_name, seat_label FROM passengers WHERE id = %s", (passenger_id,))
    p = cur.fetchone()
    text = f"{p['passenger_name']} ({p['seat_label']})" + (f" — {note}" if note else "")
    cur.execute("""INSERT INTO trip_events (trip_id, event_type, location, event_time, note)
                   VALUES (%s,'alight',%s,%s,%s)""", (trip_id, location, now, text[:255]))
    return p


def compute_kpis(cur, day):
    """The numbers behind the dashboard's KPI cards for one date (used by /api/kpis and the day PDF)."""
    cur.execute("""
        SELECT COUNT(*) AS total_trips,
               COUNT(DISTINCT CASE WHEN t.status <> 'cancelled' THEN t.vehicle_id END) AS vehicles_used,
               COALESCE(SUM(t.status = 'arrived'), 0) AS arrived_trips,
               COALESCE(SUM(t.status = 'arrived' AND t.delay_minutes <= %s), 0) AS on_time_trips,
               COALESCE(SUM(t.delay_minutes > 0 OR (t.status IN ('moving','paused','stopped')
                            AND TIMESTAMPDIFF(MINUTE, t.scheduled_arrival, %s) > 0)), 0) AS delayed_trips,
               COALESCE(SUM(t.delay_minutes), 0) AS total_delay_minutes,
               AVG(CASE WHEN t.status = 'arrived' THEN t.delay_minutes END) AS avg_delay_minutes,
               AVG(t.estimated_duration_minutes) AS avg_estimated_duration_minutes,
               COALESCE(SUM(CASE WHEN t.status <> 'cancelled' THEN v.seat_count END), 0) AS seat_capacity
        FROM trips t JOIN vehicles v ON v.id = t.vehicle_id
        WHERE t.trip_date = %s
    """, (ON_TIME_GRACE_MINUTES, datetime.now(), day))
    k = cur.fetchone()
    cur.execute("""
        SELECT COUNT(*) AS booked, COUNT(p.alighted_at) AS alighted
        FROM passengers p JOIN trips t ON t.id = p.trip_id
        WHERE t.trip_date = %s AND t.status <> 'cancelled'
    """, (day,))
    p = cur.fetchone()
    summary = trip_status_summary(cur, day)

    arrived = int(k['arrived_trips'])
    capacity = int(k['seat_capacity'])
    return {
        'date': day.isoformat(),
        'total_trips': k['total_trips'],
        'trips_by_status': {s: summary[s] for s in
                            ('scheduled', 'moving', 'paused', 'stopped', 'arrived', 'cancelled')},
        'vehicles_used': k['vehicles_used'],
        'vehicles_on_the_road': summary['moving'] + summary['paused'] + summary['stopped'],
        'delayed_trips': int(k['delayed_trips']),
        'total_delay_minutes': int(k['total_delay_minutes']),
        'avg_delay_minutes': _num(k['avg_delay_minutes'], 1),
        'avg_estimated_duration_minutes': _num(k['avg_estimated_duration_minutes'], 1),
        'on_time_rate_pct': round(int(k['on_time_trips']) * 100 / arrived, 1) if arrived else None,
        'passengers_booked': p['booked'],
        'passengers_alighted': p['alighted'],
        'passengers_onboard': p['booked'] - p['alighted'],
        'seat_occupancy_pct': round(p['booked'] * 100 / capacity, 1) if capacity else None,
    }


# ── PDF REPORTS (black and white, tables only, Helvetica) ──────────────
PDF_MARGIN = 15 * mm
PDF_WIDTH = A4[0] - 2 * PDF_MARGIN
VIEWER_FILE_PREFIX = re.sub(r'\W+', '_', config.COMPANY_NAME).strip('_')   # "GUO Transport" -> GUO_Transport_Report_<date>.pdf (viewers)
FILE_PREFIX = config.COMPANY_NAME.split()[0]            # "GUO" -> GUO_Trip_Report_<plate>_<date>.pdf


# emoji, pictographs, dingbats, variation selectors and joiners (the "→" arrow is handled separately)
_EMOJI_RE = re.compile('[🀀-🫿☀-➿⬀-⯿⌀-⏿←-⇿︀-️‍⃣]')


def strip_emoji(value):
    """Plain text only: emoji and pictographs are removed (user-typed names and notes may contain them)."""
    s = '' if value is None else str(value)
    s = s.replace('→', 'to')
    return re.sub(r' {2,}', ' ', _EMOJI_RE.sub('', s)).strip() if _EMOJI_RE.search(s) else s


def _pdf_text(value):
    """Safe text for a Paragraph: the built-in PDF fonts are WinAnsi (no arrows, no emoji)."""
    s = strip_emoji(value).replace('—', '-').replace('–', '-')
    return escape(s.encode('cp1252', 'replace').decode('cp1252'))


def _pdf_styles():
    base = dict(fontName='Helvetica', textColor=colors.black)
    return {
        'company': ParagraphStyle('company', fontName='Helvetica-Bold', fontSize=22, leading=26, textColor=colors.black),
        'title':   ParagraphStyle('title', fontSize=13, leading=17, spaceAfter=2, **base),
        'meta':    ParagraphStyle('meta', fontSize=9, leading=12, spaceAfter=6, **base),
        'h2':      ParagraphStyle('h2', fontName='Helvetica-Bold', fontSize=13, leading=16, spaceBefore=4, spaceAfter=4, textColor=colors.black),
        'h3':      ParagraphStyle('h3', fontName='Helvetica-Bold', fontSize=10.5, leading=13, spaceBefore=10, spaceAfter=4, textColor=colors.black),
        'cell':    ParagraphStyle('cell', fontSize=8.5, leading=10.5, **base),
        'cellb':   ParagraphStyle('cellb', fontName='Helvetica-Bold', fontSize=8.5, leading=10.5, textColor=colors.black),
        'note':    ParagraphStyle('note', fontSize=9, leading=12, spaceAfter=4, **base),
    }


def _pdf_table(rows, widths, st, header=True, label_col=False):
    """rows are lists of plain strings; they become wrapped Paragraphs. Black grid, no fills."""
    data = []
    for r, row in enumerate(rows):
        bold = (header and r == 0)
        data.append([Paragraph(_pdf_text(c), st['cellb'] if bold or (label_col and i == 0) else st['cell'])
                     for i, c in enumerate(row)])
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    style = [('GRID', (0, 0), (-1, -1), 0.5, colors.black), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
             ('LEFTPADDING', (0, 0), (-1, -1), 5), ('RIGHTPADDING', (0, 0), (-1, -1), 5),
             ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3)]
    if header:
        style.append(('LINEBELOW', (0, 0), (-1, 0), 1.5, colors.black))
    t.setStyle(TableStyle(style))
    return t


def _pdf_footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(colors.black)
    canvas.setLineWidth(0.5)
    canvas.line(PDF_MARGIN, 14 * mm, A4[0] - PDF_MARGIN, 14 * mm)
    canvas.setFont('Helvetica', 8)
    canvas.drawString(PDF_MARGIN, 10 * mm, f"{config.COMPANY_NAME} - generated {datetime.now():%Y-%m-%d %H:%M}")
    canvas.drawRightString(A4[0] - PDF_MARGIN, 10 * mm, f"Page {doc.page}")
    canvas.restoreState()


def _pdf_build(story, title):
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=PDF_MARGIN, rightMargin=PDF_MARGIN, topMargin=PDF_MARGIN,
                            bottomMargin=20 * mm, title=title, author=config.COMPANY_NAME)
    doc.build(story, onFirstPage=_pdf_footer, onLaterPages=_pdf_footer)
    return buf.getvalue()


def _short_dt(value, day=None):
    """'2026-10-26 12:15:30' -> '2026-10-26 12:15'; '-' when empty."""
    return value[:16] if value else '-'


def _trip_flowables(t, st, viewer=False):
    """Details table, passengers table and events table for one trip. For a viewer the passengers section is a count
    only (no names, seats or drop-offs) and the internal event log is left out."""
    if t['status'] == 'arrived':
        delay = str(t['delay_minutes'])
    elif t['status'] in IN_PROGRESS:
        delay = f"{t['late_minutes']} (late so far)" if t['late_minutes'] else '0'
    else:
        delay = '-'
    details = [
        ['Vehicle', f"{t['model_name']} ({t['plate_number']})"],
        ['Driver', t['driver_name']],
        ['Route', f"{t['origin']} to {t['destination']} (Point A to Point B)"],
        ['Seats', str(t['seat_count'])],
        ['Scheduled departure', _short_dt(t['scheduled_departure'])],
        ['Actual departure', _short_dt(t['actual_departure']) if t['actual_departure'] else 'not departed'],
        ['Scheduled arrival', _short_dt(t['scheduled_arrival'])],
        ['Actual arrival', _short_dt(t['actual_arrival']) if t['actual_arrival'] else 'not arrived'],
        ['Status', t['status'].upper()],
        ['Delay (minutes)', delay],
        ['Delay reason', t['delay_reason'] or '-'],
    ]
    out = [_pdf_table(details, [45 * mm, PDF_WIDTH - 45 * mm], st, header=False, label_col=True)]

    if viewer:
        total, off = t['passengers_total'], t['passengers_alighted']
        out.append(Paragraph(f"Passengers ({total})", st['h3']))
        if not total:
            out.append(Paragraph('No passengers booked.', st['note']))
        elif t['status'] in ('scheduled', 'cancelled'):
            out.append(Paragraph(f"{total} passenger{'' if total == 1 else 's'} booked.", st['note']))
        else:
            out.append(Paragraph(f"{total} passenger{'' if total == 1 else 's'} booked: {off} dropped off, {total - off} not yet.", st['note']))
        return out

    out.append(Paragraph(f"Passengers ({len(t['passengers'])})", st['h3']))
    if t['passengers']:
        waiting = {'scheduled': 'booked, not departed', 'cancelled': 'trip cancelled'}.get(t['status'], 'still on board')
        rows = [['Name', 'Seat', 'Planned drop-off', 'Got off (where and when)']]
        for p in t['passengers']:
            when = (f"{p['alighted_location'] or p['drop_off_location']}, "
                    f"{p['alighted_at'][11:16] if p['alighted_at'][:10] == t['trip_date'] else p['alighted_at'][:16]}"
                    if p['alighted_at'] else waiting)
            rows.append([p['passenger_name'], p['seat_label'], p['drop_off_location'], when])
        out.append(_pdf_table(rows, [45 * mm, 38 * mm, 38 * mm, PDF_WIDTH - 121 * mm], st))
    else:
        out.append(Paragraph('No passengers booked.', st['note']))

    out.append(Paragraph(f"Trip events ({len(t['events'])})", st['h3']))
    rows = [['Time', 'Event', 'Location', 'Note']]
    for e in t['events']:
        rows.append([e['event_time'][11:19] if e['event_time'][:10] == t['trip_date'] else e['event_time'][:19],
                     e['event_type'], e['location'] or '-', e['note'] or '-'])
    out.append(_pdf_table(rows, [24 * mm, 24 * mm, 32 * mm, PDF_WIDTH - 80 * mm], st))
    return out


def build_trip_pdf(t, viewer=False):
    st = _pdf_styles()
    story = [Paragraph(_pdf_text(config.COMPANY_NAME), st['company']),
             Paragraph('Trip Report', st['title']),
             Paragraph(f"Report date: {_pdf_text(t['trip_date'])} &nbsp;&nbsp; Trip #{t['id']}", st['meta'])]
    story += _trip_flowables(t, st, viewer)
    return _pdf_build(story, f"{config.COMPANY_NAME} Trip Report {t['plate_number']} {t['trip_date']}")


def build_day_pdf(day, trips, k, viewer=False):
    """Page 1: the KPI numbers. Then one page section per vehicle with all of its trips that day."""
    st = _pdf_styles()
    story = [Paragraph(_pdf_text(config.COMPANY_NAME), st['company']),
             Paragraph('End of Day Report', st['title']),
             Paragraph(f"Report date: {day.isoformat()} &nbsp;&nbsp; {len(trips)} trip(s)", st['meta']),
             Paragraph('Summary', st['h2'])]
    rate = f"{k['on_time_rate_pct']}%" if k['on_time_rate_pct'] is not None else '-'
    story.append(_pdf_table([
        ['Indicator', 'Value'],
        ['Vehicles today (with a trip)', str(k['vehicles_used'])],
        ['On the road (moving, paused, stopped)', str(k['vehicles_on_the_road'])],
        ['Arrived', str(k['trips_by_status']['arrived'])],
        ['Delayed (late now or arrived late)', str(k['delayed_trips'])],
        ['On-time rate (of arrived trips)', rate],
        ['Passengers dropped off / booked', f"{k['passengers_alighted']} / {k['passengers_booked']}"],
    ], [110 * mm, PDF_WIDTH - 110 * mm], st))
    story.append(Paragraph('Trips by status', st['h3']))
    by = k['trips_by_status']
    story.append(_pdf_table([['Status', 'Trips']] + [[s.capitalize(), str(by[s])] for s in by] +
                            [['Total trips', str(k['total_trips'])], ['Total delay (minutes)', str(k['total_delay_minutes'])]],
                            [110 * mm, PDF_WIDTH - 110 * mm], st))

    groups = {}
    for t in trips:                                   # trips are already ordered by scheduled departure
        groups.setdefault(t['vehicle_id'], []).append(t)
    for ts in groups.values():
        first = ts[0]
        story.append(PageBreak())
        story.append(Paragraph(f"Vehicle: {_pdf_text(first['model_name'])} ({_pdf_text(first['plate_number'])})", st['h2']))
        story.append(Paragraph(f"Driver: {_pdf_text(first['driver_name'])} &nbsp;&nbsp; Seats: {first['seat_count']} "
                               f"&nbsp;&nbsp; Date: {day.isoformat()} &nbsp;&nbsp; Trips: {len(ts)}", st['meta']))
        for t in ts:
            story.append(Paragraph(f"Trip #{t['id']}: {_pdf_text(t['origin'])} to {_pdf_text(t['destination'])}", st['h3']))
            story += _trip_flowables(t, st, viewer)
    return _pdf_build(story, f"{config.COMPANY_NAME} End of Day Report {day.isoformat()}")


# ── PERIOD REPORTS (admin only: monthly and yearly; counts only, no passenger names, no phones) ──
MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
               'September', 'October', 'November', 'December']
# same definitions as compute_kpis: arrived = status arrived; on time = arrived with delay <= grace;
# delayed = delay_minutes > 0, or still on the road and already past its scheduled arrival
_PERIOD_AGG = """COUNT(*) AS trips,
        COALESCE(SUM(t.status = 'arrived'), 0) AS arrived,
        COALESCE(SUM(t.status = 'arrived' AND t.delay_minutes <= %s), 0) AS on_time,
        COALESCE(SUM(t.delay_minutes > 0 OR (t.status IN ('moving','paused','stopped')
                     AND TIMESTAMPDIFF(MINUTE, t.scheduled_arrival, %s) > 0)), 0) AS `delayed`"""
_PERIOD_GROUPS = {'day': 't.trip_date', 'month': 'MONTH(t.trip_date)', 'vehicle': 't.vehicle_id'}


def parse_period(kind, value):
    """'2026-11' (month) or '2026' (year) -> (start date, end date exclusive, label). Raises ValueError with a clear message."""
    text = value.strip() if isinstance(value, str) else ''
    if kind == 'month':
        m = re.fullmatch(r'(\d{4})-(\d{2})', text)
        if not m or not 1 <= int(m.group(2)) <= 12 or not 2000 <= int(m.group(1)) <= 2100:
            raise ValueError('month must look like 2026-11 (year 2000-2100, month 01-12)')
        y, mo = int(m.group(1)), int(m.group(2))
        end = date(y + 1, 1, 1) if mo == 12 else date(y, mo + 1, 1)
        return date(y, mo, 1), end, f"{MONTH_NAMES[mo - 1]} {y}"
    if not re.fullmatch(r'\d{4}', text) or not 2000 <= int(text) <= 2100:
        raise ValueError('year must be four digits between 2000 and 2100, like 2026')
    y = int(text)
    return date(y, 1, 1), date(y + 1, 1, 1), str(y)


def period_stats(cur, start, end, group):
    """{group key: {trips, arrived, on_time, delayed, booked, dropped}} for non-cancelled trips in [start, end)."""
    key = _PERIOD_GROUPS[group]
    where = "t.trip_date >= %s AND t.trip_date < %s AND t.status <> 'cancelled'"
    cur.execute(f"SELECT {key} AS k, {_PERIOD_AGG} FROM trips t WHERE {where} GROUP BY k ORDER BY k",
                (ON_TIME_GRACE_MINUTES, datetime.now(), start, end))
    out = {r['k']: {'trips': int(r['trips']), 'arrived': int(r['arrived']), 'on_time': int(r['on_time']),
                    'delayed': int(r['delayed']), 'booked': 0, 'dropped': 0} for r in cur.fetchall()}
    cur.execute(f"""SELECT {key} AS k, COUNT(p.id) AS booked, COUNT(p.alighted_at) AS dropped
                    FROM passengers p JOIN trips t ON t.id = p.trip_id WHERE {where} GROUP BY k""", (start, end))
    for r in cur.fetchall():
        if r['k'] in out:
            out[r['k']]['booked'], out[r['k']]['dropped'] = int(r['booked']), int(r['dropped'])
    return out


def period_report_data(cur, start, end, group):
    """Everything both reports print. None when the period has no trips."""
    rows = period_stats(cur, start, end, group)
    if not rows:
        return None
    total = {f: sum(r[f] for r in rows.values()) for f in ('trips', 'arrived', 'on_time', 'delayed', 'booked', 'dropped')}
    cur.execute("""SELECT COUNT(t.distance) AS n, COALESCE(SUM(t.distance), 0) AS km FROM trips t
                   WHERE t.trip_date >= %s AND t.trip_date < %s AND t.status <> 'cancelled'""", (start, end))
    d = cur.fetchone()
    total['distance_km'] = round(float(d['km']), 1) if d['n'] else None      # only when the data exists
    vehicles = period_stats(cur, start, end, 'vehicle')
    cur.execute("""SELECT v.id, v.model_name, v.plate_number,
                          GROUP_CONCAT(DISTINCT COALESCE(t.driver_name, v.driver_name) SEPARATOR ', ') AS drivers
                   FROM trips t JOIN vehicles v ON v.id = t.vehicle_id
                   WHERE t.trip_date >= %s AND t.trip_date < %s AND t.status <> 'cancelled'
                   GROUP BY v.id, v.model_name, v.plate_number ORDER BY v.model_name, v.plate_number""", (start, end))
    per_vehicle = [dict(r, **vehicles[r['id']]) for r in cur.fetchall() if r['id'] in vehicles]
    return {'rows': rows, 'total': total, 'vehicles': per_vehicle}


def _pct(on_time, arrived):
    return f"{round(on_time * 100 / arrived, 1)}%" if arrived else '-'


def build_period_pdf(kind, label, data):
    st = _pdf_styles()
    t = data['total']
    story = [Paragraph(_pdf_text(config.COMPANY_NAME), st['company']),
             Paragraph('Monthly Report' if kind == 'month' else 'Yearly Report', st['title']),
             Paragraph(f"Period: {_pdf_text(label)} &nbsp;&nbsp; {t['trips']} trip(s)", st['meta']),
             Paragraph('Summary', st['h2'])]
    summary = [['Indicator', 'Value'],
               ['Total trips (cancelled trips excluded)', str(t['trips'])],
               ['Completed (arrived)', str(t['arrived'])],
               ['Delayed (late now or arrived late)', str(t['delayed'])],
               ['On-time rate (of arrived trips)', _pct(t['on_time'], t['arrived'])],
               ['Passengers booked', str(t['booked'])],
               ['Passengers dropped off', str(t['dropped'])]]
    if t['distance_km'] is not None:
        summary.append(['Total distance (km)', f"{t['distance_km']:g}"])
    story.append(_pdf_table(summary, [110 * mm, PDF_WIDTH - 110 * mm], st))

    rows = data['rows']
    if kind == 'month':
        story.append(Paragraph('Trips per day (days with at least one trip)', st['h3']))
        table = [['Date', 'Trips', 'Arrived', 'Delayed', 'Passengers booked']]
        table += [[d.isoformat(), str(r['trips']), str(r['arrived']), str(r['delayed']), str(r['booked'])]
                  for d, r in rows.items()]
        widths = [50 * mm] + [(PDF_WIDTH - 50 * mm) / 4] * 4
    else:
        story.append(Paragraph('Trips per month (months with at least one trip)', st['h3']))
        table = [['Month', 'Trips', 'Arrived', 'Delayed', 'On-time %', 'Passengers booked']]
        table += [[MONTH_NAMES[m - 1], str(r['trips']), str(r['arrived']), str(r['delayed']),
                   _pct(r['on_time'], r['arrived']), str(r['booked'])] for m, r in rows.items()]
        widths = [40 * mm] + [(PDF_WIDTH - 40 * mm) / 5] * 5
    story.append(_pdf_table(table, widths, st))

    story.append(Paragraph('Trips per vehicle', st['h3']))
    table = [['Vehicle', 'Plate', 'Driver', 'Trips', 'Arrived', 'Delayed', 'On-time %']]
    table += [[v['model_name'], v['plate_number'], v['drivers'] or '-', str(v['trips']), str(v['arrived']),
               str(v['delayed']), _pct(v['on_time'], v['arrived'])] for v in data['vehicles']]
    story.append(_pdf_table(table, [34 * mm, 28 * mm, 40 * mm, 16 * mm, 20 * mm, 20 * mm, PDF_WIDTH - 158 * mm], st))
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph('Counts only: passenger names and contact details are not part of this report.', st['note']))
    return _pdf_build(story, f"{config.COMPANY_NAME} {'Monthly' if kind == 'month' else 'Yearly'} Report {label}")


# ── EMAIL (share a report PDF) ───────────────────────────────────────
ENV_PATH = Path(__file__).resolve().parent / '.env'
SMTP_KEYS = ('SMTP_HOST', 'SMTP_PORT', 'SMTP_USER', 'SMTP_PASSWORD')
SMTP_TIMEOUT = 15                       # seconds
EMAIL_LIMIT, EMAIL_WINDOW = 5, 60       # at most 5 emails per minute per IP
# one address only: no spaces, commas, semicolons, angle brackets or line breaks can match
EMAIL_RE = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
                      r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+")
_email_hits = defaultdict(deque)
_email_lock = threading.Lock()


def email_problem(value):
    """None if `value` is exactly one valid address, otherwise why not. Never strips or fixes the input."""
    if not isinstance(value, str) or not value:
        return 'Enter an email address.'
    if re.search(r'[\s,;<>]', value):
        return 'Enter exactly one email address (no commas, spaces or line breaks).'
    local = value.rpartition('@')[0]
    if len(value) > 254 or len(local) > 64 or not EMAIL_RE.fullmatch(value):
        return 'That is not a valid email address.'
    return None


def smtp_settings():
    """(settings, missing). Read from .env on every call, so editing the file needs no restart; real
    environment variables win over the file. Nothing is hard-coded and no value is ever logged or returned."""
    from_file = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    cfg = {k: (os.environ.get(k) or from_file.get(k) or '').strip() for k in SMTP_KEYS + ('SENDER_NAME',)}
    missing = [k for k in SMTP_KEYS if not cfg[k]]
    if not missing:
        try:
            cfg['SMTP_PORT'] = int(cfg['SMTP_PORT'])
        except ValueError:
            missing.append('SMTP_PORT (must be a number)')
    return (None, missing) if missing else (cfg, [])


def email_wait_seconds(ip):
    """0 and records the attempt if `ip` is under the limit; otherwise the seconds until it may send again."""
    now = time.monotonic()
    with _email_lock:
        hits = _email_hits[ip]
        while hits and now - hits[0] >= EMAIL_WINDOW:
            hits.popleft()
        if len(hits) >= EMAIL_LIMIT:
            return int(EMAIL_WINDOW - (now - hits[0])) + 1
        hits.append(now)
        if len(_email_hits) > 500:                       # forget IPs that have gone quiet
            for k in [k for k, d in _email_hits.items() if not d or now - d[-1] >= EMAIL_WINDOW]:
                del _email_hits[k]
        return 0


def deliver_report(cfg, to, subject, body, pdf, filename):
    """Send one email with the PDF attached, SMTP over SSL. Returns (ok, error_message, http_status).
    Never raises: a failed send must not take the app (or the simulator thread) down."""
    try:
        msg = EmailMessage()
        msg['From'] = formataddr((cfg['SENDER_NAME'] or config.COMPANY_NAME, cfg['SMTP_USER']))
        msg['To'] = to
        msg['Subject'] = subject
        msg.set_content(body)
        msg.add_attachment(pdf, maintype='application', subtype='pdf', filename=filename)
        with smtplib.SMTP_SSL(cfg['SMTP_HOST'], cfg['SMTP_PORT'], timeout=SMTP_TIMEOUT,
                              context=ssl.create_default_context()) as smtp:
            smtp.login(cfg['SMTP_USER'], cfg['SMTP_PASSWORD'])
            smtp.send_message(msg)
        return True, None, 200
    except smtplib.SMTPAuthenticationError:
        return False, 'could not send: the mail server refused the login (check SMTP_USER and SMTP_PASSWORD in .env)', 502
    except smtplib.SMTPRecipientsRefused:
        return False, 'could not send: the mail server refused that recipient address', 502
    except smtplib.SMTPSenderRefused:
        return False, 'could not send: the mail server refused the sender address (check SMTP_USER in .env)', 502
    except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError):
        return False, 'could not send, check your internet connection', 502
    except smtplib.SMTPException:
        return False, 'could not send: the mail server returned an error', 502
    except (OSError, ssl.SSLError):                      # DNS failure, refused/timed-out connection, TLS problem
        return False, 'could not send, check your internet connection', 502
    except Exception:
        return False, 'could not send the email (unexpected error)', 500


def _pdf_response(data, filename):
    resp = Response(data, mimetype='application/pdf')
    resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    resp.headers['Cache-Control'] = 'no-store'
    resp.headers['Access-Control-Expose-Headers'] = 'Content-Disposition'   # lets the dashboard read the file name
    return resp


# ── ROUTES ───────────────────────────────────────
def register_trip_routes(app, mysql):

    def dict_cursor():
        return mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    def day_arg():
        return parse_date_arg(request.args.get('date'))

    @app.before_request
    def drivers_schema_guard():
        """If MySQL was down when the app started, the drivers table is created on the first API request that finds it up."""
        if not _driver_schema['ready'] and request.path.startswith('/api/') and request.path != '/api/health':
            try:
                ensure_driver_schema(mysql.connection)
            except Exception:
                app.logger.warning('could not prepare the drivers table yet', exc_info=True)

    # ---------- create a trip (+ vehicle if the plate is new, + passengers) ----------
    @app.route('/api/trips', methods=['POST'])
    @admin_required
    def create_trip():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return _error('Request body must be a JSON object')

        errors = []
        plate_raw = _text(data.get('plate_number'), 100)
        if not plate_raw:
            errors.append('plate_number is required')
        elif not PLATE_RE.fullmatch(plate_raw):
            errors.append('plate_number must be 3 to 15 characters: letters, digits and hyphens only')
        plate = plate_raw.upper()

        towns = ', '.join(config.TOWNS)
        origin, destination = _town(data.get('origin')), _town(data.get('destination'))
        if origin is None:
            errors.append(f'origin must be one of: {towns}')
        if destination is None:
            errors.append(f'destination must be one of: {towns}')
        if origin and origin == destination:
            errors.append('origin and destination must be different towns')

        departure = arrival = None
        try:
            departure = _parse_datetime(data.get('scheduled_departure'), 'scheduled_departure')
        except ValueError as e:
            errors.append(str(e))
        try:
            arrival = _parse_datetime(data.get('scheduled_arrival'), 'scheduled_arrival')
        except ValueError as e:
            errors.append(str(e))
        if departure and arrival and arrival <= departure:
            errors.append('scheduled_arrival must be after scheduled_departure')

        passengers_in = data.get('passengers', [])
        if not isinstance(passengers_in, list):
            errors.append('passengers must be a list')
            passengers_in = []

        driver_id = data.get('driver_id')
        if isinstance(driver_id, bool) or not isinstance(driver_id, int):
            errors.append('driver_id is required: choose a driver')
            driver_id = None

        cur = dict_cursor()

        # Existing vehicle (by plate) or details for a new one
        vehicle = None
        if PLATE_RE.fullmatch(plate_raw):
            cur.execute("""SELECT id, model_name, seat_count, removed_at
                           FROM vehicles WHERE plate_number = %s""", (plate,))
            vehicle = cur.fetchone()

        if vehicle:
            seat_count = vehicle['seat_count']
            if not seat_count:
                errors.append('vehicle has no seat_count on record; update the vehicle first')
        else:
            model_name  = _text(data.get('model_name'), 100)
            seat_count  = data.get('seat_count')
            if not model_name:  errors.append('model_name is required for a new plate')
            if isinstance(seat_count, bool) or not isinstance(seat_count, int) \
                    or not 1 <= seat_count <= MAX_SEATS:
                errors.append(f'seat_count must be a whole number from 1 to {MAX_SEATS} for a new plate')
                seat_count = None

        # Passengers: count, names, seats
        assigned = []
        if seat_count:
            layout = seat_labels(seat_count)
            by_key = {_seat_key(s): s for s in layout}
            if len(passengers_in) > seat_count:
                errors.append(f'{len(passengers_in)} passengers exceed the vehicle capacity of {seat_count} seats')
            else:
                used, pending = set(), []
                for i, p in enumerate(passengers_in, 1):
                    if not isinstance(p, dict):
                        errors.append(f'passenger {i} must be an object')
                        continue
                    name = _text(p.get('passenger_name'), 100)
                    if not name:
                        errors.append(f'passenger {i}: passenger_name is required')
                    drop = _text(p.get('drop_off_location'), 100) or destination or ''
                    seat = None
                    if p.get('seat_label') not in (None, ''):
                        seat = by_key.get(_seat_key(p['seat_label']))
                        if seat is None:
                            errors.append(f"passenger {i}: seat '{p['seat_label']}' is not in this layout")
                        elif seat in used:
                            errors.append(f"passenger {i}: seat '{seat}' is already taken by another passenger")
                        else:
                            used.add(seat)
                    pending.append({'passenger_name': name, 'seat_label': seat, 'drop_off_location': drop})
                free = (s for s in layout if s not in used)   # passengers without a seat get the next free one
                for p in pending:
                    if p['seat_label'] is None:
                        p['seat_label'] = next(free, None)
                assigned = pending

        if errors:
            cur.close()
            return _error('Validation failed', 400, details=errors)

        if vehicle and vehicle['removed_at']:
            cur.close()
            return _error(removed_plate_message(plate, vehicle['model_name']), 409, plate_number=plate, removed=True)
        if vehicle:                     # the plate identifies the vehicle; never silently ignore a different name or seat count
            conflict = registered_mismatch(plate, vehicle, data)
            if conflict:
                cur.close()
                return _error(conflict, 409, registered={'plate_number': plate, 'model_name': vehicle['model_name'],
                                                         'seat_count': vehicle['seat_count']})
        duration = int((arrival - departure).total_seconds() // 60)
        try:
            if vehicle:
                vehicle_id = vehicle['id']
                # Lock the vehicle, then look for a clash with current data (FOR UPDATE is a current
                # read, so a trip committed by a concurrent request is seen, not hidden by our snapshot).
                cur.execute("SELECT id, removed_at FROM vehicles WHERE id = %s FOR UPDATE", (vehicle_id,))
                locked = cur.fetchone()
                if locked is None or locked['removed_at']:           # removed while this request was running
                    mysql.connection.rollback()
                    cur.close()
                    return _error(removed_plate_message(plate, vehicle['model_name']), 409, plate_number=plate, removed=True)
                cur.execute("""
                    SELECT id, origin, destination, scheduled_departure, scheduled_arrival, status
                    FROM trips
                    WHERE vehicle_id = %s AND status <> 'cancelled'
                      AND scheduled_departure < %s AND scheduled_arrival > %s
                    ORDER BY scheduled_departure LIMIT 1 FOR UPDATE
                """, (vehicle_id, arrival, departure))
                clash = cur.fetchone()
                if clash:
                    mysql.connection.rollback()
                    cur.close()
                    return _error(
                        f"Vehicle {plate} already has trip #{clash['id']} ({clash['origin']} to "
                        f"{clash['destination']}, {clash['status']}) from "
                        f"{clash['scheduled_departure']:%Y-%m-%d %H:%M} to "
                        f"{clash['scheduled_arrival']:%H:%M}, which overlaps the requested "
                        f"{departure:%Y-%m-%d %H:%M} to {arrival:%H:%M}.",
                        409, conflicting_trip_id=clash['id'])
            else:
                start = config.TOWNS[origin]
                cur.execute("""
                    INSERT INTO vehicles
                    (plate_number, model_name, seat_count,
                     name, plate, status, speed, fuel, latitude, longitude, trips)
                    VALUES (%s,%s,%s, %s,%s,'idle',0,100,%s,%s,0)
                """, (plate, model_name, seat_count,
                      model_name, plate, start['lat'], start['lng']))
                vehicle_id = cur.lastrowid

            # the driver: lock the row (so a concurrent "make inactive" or second booking waits), then read it as it is now
            cur.execute("SELECT id, full_name, status, removed_at FROM drivers WHERE id = %s FOR UPDATE", (driver_id,))
            drv = cur.fetchone()
            if drv is None or drv['removed_at'] or drv['status'] != 'active':
                mysql.connection.rollback()
                cur.close()
                return _error('Validation failed', 400, details=[
                    'The chosen driver does not exist' if drv is None
                    else f"Driver {drv['full_name']} was removed. Choose another driver." if drv['removed_at']
                    else f"Driver {drv['full_name']} is inactive. Choose an active driver."])
            dclash = driver_clash(cur, driver_id, departure, arrival)
            if dclash:
                mysql.connection.rollback()
                cur.close()
                return _error(driver_clash_message(drv['full_name'], dclash, departure, arrival), 409,
                              conflicting_trip_id=dclash['id'])

            cur.execute("""
                INSERT INTO trips
                (vehicle_id, driver_id, driver_name, origin, destination, scheduled_departure, scheduled_arrival,
                 estimated_duration_minutes, status, trip_date)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'scheduled',%s)
            """, (vehicle_id, driver_id, drv['full_name'], origin, destination, departure, arrival, duration, departure.date()))
            trip_id = cur.lastrowid

            if assigned:
                cur.executemany("""
                    INSERT INTO passengers (trip_id, passenger_name, seat_label, drop_off_location)
                    VALUES (%s,%s,%s,%s)
                """, [(trip_id, p['passenger_name'], p['seat_label'], p['drop_off_location']) for p in assigned])

            cur.execute("""
                INSERT INTO trip_events (trip_id, event_type, location, event_time, note)
                VALUES (%s,'created',%s,%s,%s)
            """, (trip_id, origin, datetime.now().replace(microsecond=0),
                  f'{len(assigned)} passenger(s) booked'))
            mysql.connection.commit()
        except MySQLdb.IntegrityError as e:
            mysql.connection.rollback()
            cur.close()
            if e.args and e.args[0] == DUP_KEY:
                return _error('That plate_number was just registered by another request; try again', 409)
            app.logger.exception('create_trip integrity error')
            return _error('Could not save the trip', 500)
        except Exception:
            mysql.connection.rollback()
            cur.close()
            app.logger.exception('create_trip failed')
            return _error('Could not save the trip', 500)

        trip = _load_trips(cur, ' WHERE t.id = %s', (trip_id,))[0]
        cur.close()
        trip['seat_layout'] = seat_labels(seat_count)
        return jsonify(trip), 201

    # ---------- FLEET ----------
    @app.route('/api/fleet')
    @login_required
    def get_fleet():
        cur = dict_cursor()
        rows = fleet_rows(cur)
        cur.close()
        return jsonify(rows)

    @app.route('/api/fleet', methods=['POST'])
    @admin_required
    def register_fleet_vehicle():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return _error('Request body must be a JSON object')
        v, errors = vehicle_input(data)
        if errors:
            return _error('Validation failed', 400, details=errors)
        cur = dict_cursor()
        cur.execute("SELECT model_name, removed_at FROM vehicles WHERE plate_number = %s", (v['plate_number'],))
        existing = cur.fetchone()
        if existing:
            cur.close()
            if existing['removed_at']:
                return _error(removed_plate_message(v['plate_number'], existing['model_name']), 409,
                              plate_number=v['plate_number'], removed=True)
            return _error(f"This plate number is already registered to {existing['model_name']}", 409,
                          plate_number=v['plate_number'])
        try:
            insert_vehicle(cur, v)
            mysql.connection.commit()
        except MySQLdb.IntegrityError as e:           # two requests registered the same plate at once
            mysql.connection.rollback()
            if e.args and e.args[0] == DUP_KEY:
                cur.execute("SELECT model_name FROM vehicles WHERE plate_number = %s", (v['plate_number'],))
                other = cur.fetchone()
                cur.close()
                return _error(f"This plate number is already registered to {other['model_name'] if other else 'another vehicle'}", 409)
            cur.close()
            app.logger.exception('register vehicle failed')
            return _error('Could not register the vehicle', 500)
        except Exception:
            mysql.connection.rollback()
            cur.close()
            app.logger.exception('register vehicle failed')
            return _error('Could not register the vehicle', 500)
        cur.close()
        return jsonify({k: v[k] for k in ('plate_number', 'model_name', 'seat_count')}), 201

    # ---------- list trips for a day ----------
    @app.route('/api/trips')
    @login_required
    def list_trips():
        try:
            day = day_arg()
        except ValueError as e:
            return _error(str(e))
        cur = dict_cursor()
        trips = _load_trips(cur, " WHERE t.trip_date = %s AND t.status <> 'cancelled'", (day,))
        cur.close()
        if is_viewer():                                    # viewers get counts only, no passenger details
            strip_for_viewer(trips)
        return jsonify({'date': day.isoformat(), 'count': len(trips), 'trips': trips})

    # ---------- status changes (one guarded UPDATE each) ----------
    def reject_transition(cur, trip_id, event_type, allowed):
        """The guarded UPDATE matched nothing: say why (404 unknown trip / 409 wrong state)."""
        mysql.connection.rollback()
        cur.execute("SELECT status FROM trips WHERE id = %s", (trip_id,))
        row = cur.fetchone()
        cur.close()
        if row is None:
            return _error('Trip not found', 404)
        return _error(f"Cannot {event_type} a trip that is {row['status']}", 409,
                      status=row['status'], allowed_from=list(allowed))

    def change_status(trip_id, event_type, allowed, new_status, extra_sql='', extra_params=(), on_success=None):
        data = request.get_json(silent=True) or {}
        location = _text(data.get('location'), 100) or None
        note     = _text(data.get('note') or data.get('reason'), 255) or None
        now      = datetime.now().replace(microsecond=0)

        cur = dict_cursor()
        cur.execute("SELECT vehicle_id, origin, destination FROM trips WHERE id = %s", (trip_id,))
        trip = cur.fetchone()
        if trip is None:
            cur.close()
            return _error('Trip not found', 404)

        marks = ','.join(['%s'] * len(allowed))
        cur.execute(f"UPDATE trips SET status = %s{extra_sql} WHERE id = %s AND status IN ({marks})",
                    (new_status, *extra_params, trip_id, *allowed))
        if cur.rowcount == 0:
            return reject_transition(cur, trip_id, event_type, allowed)
        if on_success:
            on_success(cur, trip)
        cur.execute("""INSERT INTO trip_events (trip_id, event_type, location, event_time, note)
                       VALUES (%s,%s,%s,%s,%s)""", (trip_id, event_type, location, now, note))
        mysql.connection.commit()
        result = _load_trips(cur, ' WHERE t.id = %s', (trip_id,))[0]
        cur.close()
        return jsonify(result)

    @app.route('/api/trips/<int:trip_id>/start', methods=['POST'])
    @admin_required
    def start_trip(trip_id):
        data = request.get_json(silent=True) or {}
        now = datetime.now().replace(microsecond=0)
        cur = dict_cursor()
        cur.execute("SELECT vehicle_id, origin, destination FROM trips WHERE id = %s", (trip_id,))
        trip = cur.fetchone()
        if trip is None:
            cur.close()
            return _error('Trip not found', 404)
        result, other = begin_trip(cur, trip_id, trip['vehicle_id'], trip['origin'], trip['destination'], now,
                                   note=_text(data.get('note') or data.get('reason'), 255) or None,
                                   location=_text(data.get('location'), 100) or None)
        if result == 'busy':
            mysql.connection.rollback()
            cur.close()
            return _error(f"This vehicle is already on the road with trip #{other}", 409)
        if result == 'not_scheduled':
            return reject_transition(cur, trip_id, 'start', ('scheduled',))
        mysql.connection.commit()
        started = _load_trips(cur, ' WHERE t.id = %s', (trip_id,))[0]
        cur.close()
        return jsonify(started)

    @app.route('/api/trips/<int:trip_id>/pause', methods=['POST'])
    @admin_required
    def pause_trip(trip_id):
        return change_status(trip_id, 'pause', ('moving',), 'paused',
                             on_success=lambda cur, t: set_vehicle_state(cur, t['vehicle_id'], 'idle', 0))

    @app.route('/api/trips/<int:trip_id>/resume', methods=['POST'])
    @admin_required
    def resume_trip(trip_id):
        return change_status(trip_id, 'resume', ('paused', 'stopped'), 'moving',
                             on_success=lambda cur, t: set_vehicle_state(cur, t['vehicle_id'], 'moving', 0))

    @app.route('/api/trips/<int:trip_id>/stop', methods=['POST'])
    @admin_required
    def stop_trip(trip_id):
        return change_status(trip_id, 'stop', ('moving', 'paused'), 'stopped',
                             on_success=lambda cur, t: set_vehicle_state(cur, t['vehicle_id'], 'idle', 0))

    # ---------- cancel: scheduled only ----------
    @app.route('/api/trips/<int:trip_id>/cancel', methods=['POST'])
    @admin_required
    def cancel_trip(trip_id):
        """scheduled -> cancelled, with a 'cancelled' trip_event. Any other status is refused with 409. (The dashboard's
        Cancel button calls DELETE /api/trips/<id> instead, which removes a scheduled trip completely.)"""
        data = request.get_json(silent=True) or {}
        location = _text(data.get('location'), 100) or None
        note = _text(data.get('note') or data.get('reason'), 255) or None
        now = datetime.now().replace(microsecond=0)

        cur = dict_cursor()
        cur.execute("SELECT vehicle_id, status FROM trips WHERE id = %s FOR UPDATE", (trip_id,))
        trip = cur.fetchone()
        if trip is None:
            cur.close()
            return _error('Trip not found', 404)
        if trip['status'] != 'scheduled':
            mysql.connection.rollback()
            cur.close()
            return _error(f"Only scheduled trips can be cancelled (this one is {trip['status']})", 409,
                          status=trip['status'], allowed_from=['scheduled'])
        cur.execute("UPDATE trips SET status = 'cancelled' WHERE id = %s AND status = 'scheduled'", (trip_id,))
        if cur.rowcount == 0:
            return reject_transition(cur, trip_id, 'cancel', ('scheduled',))
        cur.execute("""INSERT INTO trip_events (trip_id, event_type, location, event_time, note)
                       VALUES (%s,'cancelled',%s,%s,%s)""", (trip_id, location, now, note))
        mysql.connection.commit()
        result = _load_trips(cur, ' WHERE t.id = %s', (trip_id,))[0]
        cur.close()
        return jsonify(result)

    # ---------- edit a SCHEDULED trip: departure/arrival, destination, passengers ----------
    @app.route('/api/trips/<int:trip_id>', methods=['PUT'])
    @admin_required
    def edit_trip(trip_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return _error('Request body must be a JSON object')

        cur = dict_cursor()
        cur.execute("SELECT vehicle_id, driver_id FROM trips WHERE id = %s", (trip_id,))
        found = cur.fetchone()
        if found is None:
            cur.close()
            return _error('Trip not found', 404)
        new_driver_id = found['driver_id']                  # optional change of driver
        if 'driver_id' in data:
            new_driver_id = data['driver_id']
            if isinstance(new_driver_id, bool) or not isinstance(new_driver_id, int):
                cur.close()
                return _error('Validation failed', 400, details=['driver_id must be the id of a driver'])
        # lock the vehicle first, then the driver, then the trip (the order create_trip uses; begin_trip only needs vehicle
        # then trip), so the simulator's auto-start can't start this trip while it is being edited
        cur.execute("SELECT id, plate_number, seat_count FROM vehicles WHERE id = %s FOR UPDATE", (found['vehicle_id'],))
        vehicle = cur.fetchone()
        drv = None
        if new_driver_id is not None:
            cur.execute("SELECT id, full_name, status, removed_at FROM drivers WHERE id = %s FOR UPDATE", (new_driver_id,))
            drv = cur.fetchone()
        cur.execute("""SELECT id, status, origin, destination, scheduled_departure, scheduled_arrival, driver_id
                       FROM trips WHERE id = %s FOR UPDATE""", (trip_id,))
        trip = cur.fetchone()
        if trip is None:
            mysql.connection.rollback()
            cur.close()
            return _error('Trip not found', 404)
        if trip['status'] != 'scheduled':
            mysql.connection.rollback()
            cur.close()
            return _error(f"Cannot edit a trip that is {trip['status']}. Only scheduled trips can be edited.", 409,
                          status=trip['status'])

        errors = []
        if new_driver_id is not None and new_driver_id != trip['driver_id']:       # a different driver must exist and be active
            if drv is None:
                errors.append('The chosen driver does not exist')
            elif drv['removed_at']:
                errors.append(f"Driver {drv['full_name']} was removed. Choose another driver.")
            elif drv['status'] != 'active':
                errors.append(f"Driver {drv['full_name']} is inactive. Choose an active driver.")
        towns = ', '.join(config.TOWNS)
        destination = trip['destination']
        raw_destination = data.get('destination', data.get('to_location'))
        if raw_destination is not None:
            destination = _town(raw_destination)
            if destination is None:
                errors.append(f'destination must be one of: {towns}')
        if destination and destination == trip['origin']:
            errors.append('origin and destination must be different towns')

        departure, arrival = trip['scheduled_departure'], trip['scheduled_arrival']
        times_ok = True
        for key in ('scheduled_departure', 'scheduled_arrival'):
            if data.get(key) is not None:
                try:
                    value = _parse_datetime(data[key], key)
                    if key == 'scheduled_departure':
                        departure = value
                    else:
                        arrival = value
                except ValueError as e:
                    errors.append(str(e))
                    times_ok = False
        if times_ok and arrival <= departure:
            errors.append('scheduled_arrival must be after scheduled_departure')

        # passengers: the list REPLACES the current one. An item with an "id" updates that passenger, an item
        # without one is added, and any current passenger missing from the list is removed.
        cur.execute("""SELECT id, passenger_name, seat_label, drop_off_location FROM passengers
                       WHERE trip_id = %s ORDER BY id""", (trip_id,))
        current = {r['id']: r for r in cur.fetchall()}
        new_list = None
        if 'passengers' in data:
            plist = data['passengers']
            if not isinstance(plist, list):
                errors.append('passengers must be a list')
            elif len(plist) > vehicle['seat_count']:
                errors.append(f"{len(plist)} passengers exceed the vehicle capacity of {vehicle['seat_count']} seats")
            else:
                layout = seat_labels(vehicle['seat_count'])
                by_key = {_seat_key(x): x for x in layout}
                used, items, seen_ids = set(), [], set()
                for i, p in enumerate(plist, 1):
                    if not isinstance(p, dict):
                        errors.append(f'passenger {i} must be an object')
                        continue
                    pid = p.get('id')
                    if pid is not None:
                        if isinstance(pid, bool) or not isinstance(pid, int) or pid not in current:
                            errors.append(f'passenger {i}: id {pid!r} is not a passenger of this trip')
                        elif pid in seen_ids:
                            errors.append(f'passenger {i}: id {pid} appears twice')
                        else:
                            seen_ids.add(pid)
                    name = _text(p.get('passenger_name'), 100)
                    if not name:
                        errors.append(f'passenger {i}: passenger_name is required')
                    drop = _text(p.get('drop_off_location'), 100) or destination or ''
                    seat = None
                    if p.get('seat_label') not in (None, ''):
                        seat = by_key.get(_seat_key(p['seat_label']))
                        if seat is None:
                            errors.append(f"passenger {i}: seat '{p['seat_label']}' is not in this layout")
                        elif seat in used:
                            errors.append(f"passenger {i}: seat '{seat}' is already taken by another passenger")
                        else:
                            used.add(seat)
                    items.append({'id': pid, 'passenger_name': name, 'seat_label': seat, 'drop_off_location': drop})
                free = (x for x in layout if x not in used)
                for it in items:
                    if it['seat_label'] is None:
                        it['seat_label'] = next(free, None)
                new_list = items
        if errors:
            mysql.connection.rollback()
            cur.close()
            return _error('Validation failed', 400, details=errors)

        # the vehicle must be free for the (possibly new) time window
        cur.execute("""
            SELECT id, origin, destination, scheduled_departure, scheduled_arrival, status
            FROM trips
            WHERE vehicle_id = %s AND id <> %s AND status <> 'cancelled'
              AND scheduled_departure < %s AND scheduled_arrival > %s
            ORDER BY scheduled_departure LIMIT 1 FOR UPDATE
        """, (found['vehicle_id'], trip_id, arrival, departure))
        clash = cur.fetchone()
        if clash:
            mysql.connection.rollback()
            cur.close()
            return _error(
                f"Vehicle {vehicle['plate_number']} already has trip #{clash['id']} ({clash['origin']} to "
                f"{clash['destination']}, {clash['status']}) from {clash['scheduled_departure']:%Y-%m-%d %H:%M} to "
                f"{clash['scheduled_arrival']:%H:%M}, which overlaps the requested "
                f"{departure:%Y-%m-%d %H:%M} to {arrival:%H:%M}.", 409, conflicting_trip_id=clash['id'])

        if drv is not None:                                  # the driver must be free for the (possibly new) time window
            dclash = driver_clash(cur, drv['id'], departure, arrival, exclude_trip_id=trip_id)
            if dclash:
                mysql.connection.rollback()
                cur.close()
                return _error(driver_clash_message(drv['full_name'], dclash, departure, arrival), 409,
                              conflicting_trip_id=dclash['id'])

        duration = int((arrival - departure).total_seconds() // 60)
        try:
            # No rowcount check here: the trip row was locked (FOR UPDATE) when its status was read, so it cannot have been
            # started or cancelled since, and MySQL reports 0 affected rows when an UPDATE changes nothing (e.g. saving the
            # same values again), which is not an error.
            cur.execute("""UPDATE trips SET destination = %s, scheduled_departure = %s, scheduled_arrival = %s,
                                  estimated_duration_minutes = %s, trip_date = %s
                           WHERE id = %s AND status = 'scheduled'""",
                        (destination, departure, arrival, duration, departure.date(), trip_id))
            changes = []
            if drv is not None and drv['id'] != trip['driver_id']:
                cur.execute("UPDATE trips SET driver_id = %s, driver_name = %s WHERE id = %s AND status = 'scheduled'",
                            (drv['id'], drv['full_name'], trip_id))
                changes.append(f"driver changed to {drv['full_name']}")
            if departure != trip['scheduled_departure']:
                changes.append(f"departure {trip['scheduled_departure']:%Y-%m-%d %H:%M} to {departure:%Y-%m-%d %H:%M}")
            if arrival != trip['scheduled_arrival']:
                changes.append(f"arrival {trip['scheduled_arrival']:%Y-%m-%d %H:%M} to {arrival:%Y-%m-%d %H:%M}")
            if destination != trip['destination']:
                changes.append(f"destination {trip['destination']} to {destination}")

            if new_list is not None:
                kept = [p for p in new_list if p['id'] is not None]
                new = [p for p in new_list if p['id'] is None]
                removed = set(current) - {p['id'] for p in kept}
                changed = [p for p in kept if (p['passenger_name'], p['seat_label'], p['drop_off_location']) !=
                           (current[p['id']]['passenger_name'], current[p['id']]['seat_label'],
                            current[p['id']]['drop_off_location'])]
                if removed:
                    marks = ','.join(['%s'] * len(removed))
                    cur.execute(f"DELETE FROM passengers WHERE trip_id = %s AND id IN ({marks})", (trip_id, *removed))
                if changed:
                    # park the seat labels first: swapping two passengers' seats would otherwise hit the
                    # UNIQUE (trip_id, seat_label) key half way through
                    marks = ','.join(['%s'] * len(changed))
                    cur.execute(f"UPDATE passengers SET seat_label = CONCAT('~', id) WHERE trip_id = %s AND id IN ({marks})",
                                (trip_id, *[p['id'] for p in changed]))
                    for p in changed:
                        cur.execute("""UPDATE passengers SET passenger_name = %s, seat_label = %s, drop_off_location = %s
                                       WHERE id = %s AND trip_id = %s""",
                                    (p['passenger_name'], p['seat_label'], p['drop_off_location'], p['id'], trip_id))
                if new:
                    cur.executemany("""INSERT INTO passengers (trip_id, passenger_name, seat_label, drop_off_location)
                                       VALUES (%s,%s,%s,%s)""",
                                    [(trip_id, p['passenger_name'], p['seat_label'], p['drop_off_location']) for p in new])
                if removed or changed or new:
                    changes.append(f"passengers: {len(new)} added, {len(removed)} removed, {len(changed)} changed")

            if changes:
                cur.execute("""INSERT INTO trip_events (trip_id, event_type, location, event_time, note)
                               VALUES (%s,'edited',NULL,%s,%s)""",
                            (trip_id, datetime.now().replace(microsecond=0), '; '.join(changes)[:255]))
            mysql.connection.commit()
        except Exception:
            mysql.connection.rollback()
            cur.close()
            app.logger.exception('edit_trip failed')
            return _error('Could not save the changes', 500)

        result = _load_trips(cur, ' WHERE t.id = %s', (trip_id,))[0]
        cur.close()
        result['seat_layout'] = seat_labels(vehicle['seat_count'])
        return jsonify(result)

    # ---------- delete a trip: scheduled only (this is what the dashboard's Cancel button calls) ----------
    @app.route('/api/trips/<int:trip_id>', methods=['DELETE'])
    @admin_required
    def delete_trip(trip_id):
        """Passengers first, then trip_events, then the trip (all three tables reference trips with RESTRICT)."""
        cur = dict_cursor()
        cur.execute("SELECT status FROM trips WHERE id = %s FOR UPDATE", (trip_id,))
        row = cur.fetchone()
        if row is None:
            cur.close()
            return _error('Trip not found', 404)
        if row['status'] != 'scheduled':
            mysql.connection.rollback()
            cur.close()
            return _error(f"Cannot delete a trip that is {row['status']}. Only scheduled trips can be deleted.",
                          409, status=row['status'], allowed_from=['scheduled'])
        try:
            cur.execute("DELETE FROM passengers WHERE trip_id = %s", (trip_id,))
            cur.execute("DELETE FROM trip_events WHERE trip_id = %s", (trip_id,))
            cur.execute("DELETE FROM trips WHERE id = %s AND status = 'scheduled'", (trip_id,))
            if cur.rowcount == 0:                               # its status changed under us: nothing is deleted
                return reject_transition(cur, trip_id, 'delete', ('scheduled',))
            mysql.connection.commit()
        except Exception:
            mysql.connection.rollback()
            cur.close()
            app.logger.exception('delete_trip failed')
            return _error('Could not delete the trip', 500)
        cur.close()
        return jsonify({'message': 'Trip deleted', 'trip_id': trip_id})

    @app.route('/api/trips/<int:trip_id>/arrive', methods=['POST'])
    @admin_required
    def arrive_trip(trip_id):
        data = request.get_json(silent=True) or {}
        now = datetime.now().replace(microsecond=0)
        cur = dict_cursor()
        cur.execute("SELECT vehicle_id, destination FROM trips WHERE id = %s", (trip_id,))
        trip = cur.fetchone()
        if trip is None:
            cur.close()
            return _error('Trip not found', 404)
        ok = mark_arrived(cur, trip_id, trip['vehicle_id'], trip['destination'], now,
                          delay_reason=_text(data.get('delay_reason'), 255) or None,
                          location=_text(data.get('location'), 100) or None,
                          note=_text(data.get('note'), 255) or None)
        if not ok:
            return reject_transition(cur, trip_id, 'arrive', ('moving', 'paused', 'stopped'))
        mysql.connection.commit()
        result = _load_trips(cur, ' WHERE t.id = %s', (trip_id,))[0]
        cur.close()
        return jsonify(result)

    # ---------- delay reason (editable once the trip has arrived) ----------
    @app.route('/api/trips/<int:trip_id>/delay-reason', methods=['POST'])
    @admin_required
    def set_delay_reason(trip_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get('delay_reason'), str):
            return _error('Request body must be {"delay_reason": "<text>"}')
        reason = _text(data['delay_reason'], 255) or None      # empty text clears the reason
        cur = dict_cursor()
        cur.execute("SELECT status FROM trips WHERE id = %s", (trip_id,))
        row = cur.fetchone()
        if row is None:
            cur.close()
            return _error('Trip not found', 404)
        if row['status'] != 'arrived':
            cur.close()
            return _error(f"A delay reason can only be set on an arrived trip (this one is {row['status']})", 409,
                          status=row['status'])
        # no rowcount check: MySQL reports 0 affected rows when the same reason is saved again
        cur.execute("UPDATE trips SET delay_reason = %s WHERE id = %s AND status = 'arrived'", (reason, trip_id))
        cur.execute("""INSERT INTO trip_events (trip_id, event_type, location, event_time, note)
                       VALUES (%s,'delay_reason',NULL,%s,%s)""",
                    (trip_id, datetime.now().replace(microsecond=0), reason))
        mysql.connection.commit()
        result = _load_trips(cur, ' WHERE t.id = %s', (trip_id,))[0]
        cur.close()
        return jsonify(result)

    # ---------- passenger gets off ----------
    @app.route('/api/trips/<int:trip_id>/alight', methods=['POST'])
    @admin_required
    def alight_endpoint(trip_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return _error('Request body must be a JSON object')
        passenger_id = data.get('passenger_id')
        location = _text(data.get('location'), 100)
        errors = []
        if isinstance(passenger_id, bool) or not isinstance(passenger_id, int):
            errors.append('passenger_id (whole number) is required')
        if not location:
            errors.append('location is required')
        if errors:
            return _error('Validation failed', 400, details=errors)

        cur = dict_cursor()
        cur.execute("SELECT status FROM trips WHERE id = %s", (trip_id,))
        trip = cur.fetchone()
        if trip is None:
            cur.close()
            return _error('Trip not found', 404)
        if trip['status'] in ('scheduled', 'cancelled'):
            cur.close()
            return _error(f"Passengers cannot alight from a {trip['status']} trip", 409)

        now = datetime.now().replace(microsecond=0)
        p = alight_passenger(cur, trip_id, passenger_id, location, now)
        if p is None:
            mysql.connection.rollback()
            cur.execute("SELECT alighted_at FROM passengers WHERE id = %s AND trip_id = %s",
                        (passenger_id, trip_id))
            row = cur.fetchone()
            cur.close()
            if row is None:
                return _error('Passenger not found on this trip', 404)
            return _error('Passenger has already alighted', 409, alighted_at=_clean(row['alighted_at']))
        mysql.connection.commit()
        cur.close()
        return jsonify({'message': 'Passenger alighted', 'trip_id': trip_id, 'passenger_id': passenger_id,
                        'passenger_name': p['passenger_name'], 'seat_label': p['seat_label'],
                        'location': location, 'alighted_at': _clean(now), 'alighted_location': location})

    # ---------- daily report ----------
    @app.route('/api/trips/report')
    @login_required
    def trips_report():
        try:
            day = day_arg()
        except ValueError as e:
            return _error(str(e))
        cur = dict_cursor()
        trips = _load_trips(cur, " WHERE t.trip_date = %s AND t.status <> 'cancelled'", (day,), with_events=True)
        summary = trip_status_summary(cur, day)
        cur.close()
        if is_viewer():                                    # viewers get counts only, no passenger details or event notes
            strip_for_viewer(trips)
        return jsonify({
            'date': day.isoformat(),
            'summary': summary,
            'passengers_total': sum(t['passengers_total'] for t in trips),
            'passengers_alighted': sum(t['passengers_alighted'] for t in trips),
            'trips': trips,
        })

    # ---------- PDF: one trip ----------
    @app.route('/api/trips/<int:trip_id>/report.pdf')
    @login_required
    def trip_report_pdf(trip_id):
        viewer = is_viewer()                               # viewers: no passenger details, no event log
        cur = dict_cursor()
        found = _load_trips(cur, ' WHERE t.id = %s', (trip_id,), with_events=not viewer)
        cur.close()
        if not found:
            return _error('Trip not found', 404)
        t = found[0]
        if viewer:
            strip_for_viewer(found)                        # the builder never even receives the names
        try:
            data = build_trip_pdf(t, viewer)
        except Exception:
            app.logger.exception('trip PDF failed')
            return _error('Could not build the PDF', 500)
        if viewer:
            return _pdf_response(data, f"{VIEWER_FILE_PREFIX}_Report_{t['plate_number']}_{t['trip_date']}.pdf")
        return _pdf_response(data, f"{FILE_PREFIX}_Trip_Report_{t['plate_number']}_{t['trip_date']}.pdf")

    # ---------- PDF: the whole day ----------
    @app.route('/api/trips/report.pdf')
    @login_required
    def day_report_pdf():
        try:
            day = day_arg()
        except ValueError as e:
            return _error(str(e))
        viewer = is_viewer()                               # viewers: no passenger details, no event log
        cur = dict_cursor()
        trips = _load_trips(cur, " WHERE t.trip_date = %s AND t.status <> 'cancelled'", (day,), with_events=not viewer)
        k = compute_kpis(cur, day) if trips else None
        cur.close()
        if not trips:
            return _error(f'No trips for {day.isoformat()}, so there is no report to download', 404)
        if viewer:
            strip_for_viewer(trips)                        # the builder never even receives the names
        try:
            data = build_day_pdf(day, trips, k, viewer)
        except Exception:
            app.logger.exception('day PDF failed')
            return _error('Could not build the PDF', 500)
        if viewer:
            return _pdf_response(data, f"{VIEWER_FILE_PREFIX}_Report_{day.isoformat()}.pdf")
        return _pdf_response(data, f"{FILE_PREFIX}_End_of_Day_Report_{day.isoformat()}.pdf")

    # ---------- EMAIL: share a PDF ----------
    def send_report_email(to, subject, body, build_pdf, filename):
        """Shared by both email routes: rate limit -> settings -> PDF -> send. Always answers with JSON."""
        wait = email_wait_seconds(request.remote_addr or 'unknown')
        if wait:
            return _error(f'Too many emails: the limit is {EMAIL_LIMIT} per minute. Try again in {wait} seconds.',
                          429, retry_after_seconds=wait)
        cfg, missing = smtp_settings()
        if cfg is None:
            return _error('email is not set up on this server', 503, missing=missing)
        try:
            pdf = build_pdf()
        except Exception:
            app.logger.exception('email PDF failed')
            return _error('Could not build the PDF', 500)
        ok, problem, status = deliver_report(cfg, to, strip_emoji(subject), strip_emoji(body), pdf, filename)
        if not ok:
            app.logger.warning('email to %s failed: %s', to, problem)
            return _error(problem, status)
        return jsonify({'message': f'Sent to {to}', 'email': to})

    def email_from_body():
        data = request.get_json(silent=True)
        value = data.get('email') if isinstance(data, dict) else None
        return value, email_problem(value)

    @app.route('/api/trips/<int:trip_id>/report/email', methods=['POST'])
    @admin_required
    def email_trip_report(trip_id):
        to, problem = email_from_body()
        if problem:
            return _error(problem, 400)
        cur = dict_cursor()
        found = _load_trips(cur, ' WHERE t.id = %s', (trip_id,), with_events=True)
        cur.close()
        if not found:
            return _error('Trip not found', 404)
        t = found[0]
        body = (f"Hello,\n\nPlease find the trip report attached as a PDF.\n\n"
                f"Vehicle: {t['model_name']} ({t['plate_number']})\nDriver: {t['driver_name']}\n"
                f"Route: {t['origin']} to {t['destination']}\n"
                f"Status: {t['status'].upper()}" + (f" (delay: {t['late_minutes']} min)" if t['late_minutes'] else "") +
                f"\n\n{config.COMPANY_NAME}\n")
        return send_report_email(to, f"{config.COMPANY_NAME} trip report - {t['plate_number']} - {t['trip_date']}", body,
                                 lambda: build_trip_pdf(t),
                                 f"{FILE_PREFIX}_Trip_Report_{t['plate_number']}_{t['trip_date']}.pdf")

    @app.route('/api/trips/report/email', methods=['POST'])
    @admin_required
    def email_day_report():
        to, problem = email_from_body()
        if problem:
            return _error(problem, 400)
        try:
            day = day_arg()
        except ValueError as e:
            return _error(str(e))
        cur = dict_cursor()
        trips = _load_trips(cur, " WHERE t.trip_date = %s AND t.status <> 'cancelled'", (day,), with_events=True)
        k = compute_kpis(cur, day) if trips else None
        cur.close()
        if not trips:
            return _error(f'No trips for {day.isoformat()}, so there is no report to send', 404)
        body = (f"Hello,\n\nPlease find the end of day report for {day.isoformat()} attached as a PDF.\n\n"
                f"Trips: {len(trips)}\nVehicles: {k['vehicles_used']}\nArrived: {k['trips_by_status']['arrived']}\n"
                f"Delayed: {k['delayed_trips']}\n"
                f"Passengers dropped off / booked: {k['passengers_alighted']} / {k['passengers_booked']}\n\n"
                f"{config.COMPANY_NAME}\n")
        return send_report_email(to, f"{config.COMPANY_NAME} end of day report - {day.isoformat()}", body,
                                 lambda: build_day_pdf(day, trips, k),
                                 f"{FILE_PREFIX}_End_of_Day_Report_{day.isoformat()}.pdf")

    # ---------- ADMIN: vehicles page ----------
    def admin_vehicle_rows(cur, only_id=None, removed=False):
        """Registered vehicles with the live status (idle, scheduled, or the status of the trip it is on) and the number
        of completed trips. `locked` = it has a scheduled, moving or paused trip."""
        cur.execute("""
            SELECT v.id, v.model_name AS name, v.plate_number, v.seat_count AS seats, v.removed_at,
                   (SELECT COUNT(*) FROM trips t WHERE t.vehicle_id = v.id AND t.status = 'arrived') AS completed_trips
            FROM vehicles v WHERE v.plate_number IS NOT NULL""" + (" AND v.id = %s" if only_id else
                    (" AND v.removed_at IS NOT NULL" if removed else " AND v.removed_at IS NULL")) +
                    " ORDER BY v.model_name, v.plate_number", (only_id,) if only_id else ())
        rows = cur.fetchall()
        cur.execute("SELECT vehicle_id, id, status FROM trips WHERE status IN ('scheduled','moving','paused','stopped') ORDER BY scheduled_departure")
        open_trips = {}
        for t in cur.fetchall():
            open_trips.setdefault(t['vehicle_id'], []).append(t)
        out = []
        for r in rows:
            ts = open_trips.get(r['id'], [])
            live = [t for t in ts if t['status'] in IN_PROGRESS]
            r['status'] = live[0]['status'] if live else ('scheduled' if ts else 'idle')
            r['locked'] = any(t['status'] in ('scheduled', 'moving', 'paused') for t in ts)
            out.append(_row(r))
        return out

    @app.route('/api/admin/vehicles')
    @admin_required
    def admin_vehicles():
        cur = dict_cursor()
        rows = admin_vehicle_rows(cur, removed=request.args.get('removed') in ('1', 'true'))
        cur.close()
        return jsonify(rows)

    @app.route('/api/admin/vehicles/<int:vehicle_id>', methods=['PUT'])
    @admin_required
    def admin_edit_vehicle(vehicle_id):
        """Vehicle name, plate number and seats, only while the vehicle has no scheduled, moving or paused trip (409
        otherwise). Only values that really change count. Driver fields, if sent, are ignored."""
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return _error('Request body must be a JSON object')
        cur = dict_cursor()
        cur.execute("""SELECT id, model_name, plate_number, seat_count, removed_at
                       FROM vehicles WHERE id = %s FOR UPDATE""", (vehicle_id,))       # same lock order as trip creation: vehicle first
        v = cur.fetchone()
        if v is None:
            cur.close()
            return _error('Vehicle not found', 404)
        if v['removed_at']:
            mysql.connection.rollback()
            cur.close()
            return _error(f"{v['model_name']} ({v['plate_number']}) was removed. Restore it first (Show removed, then Restore).", 409)

        errors, core = [], {}
        if 'model_name' in data:
            model = _text(data.get('model_name'), 100)
            if model:
                core['model_name'] = model
            else:
                errors.append('model_name (the vehicle name) is required')
        if 'plate_number' in data:
            plate = _text(data.get('plate_number'), 100)
            if not plate:
                errors.append('plate_number is required')
            elif not PLATE_RE.fullmatch(plate):
                errors.append('plate_number must be 3 to 15 characters: letters, digits and hyphens only')
            else:
                core['plate_number'] = plate.upper()
        if 'seat_count' in data:
            seats = data.get('seat_count')
            if isinstance(seats, bool) or not isinstance(seats, int) or not 1 <= seats <= MAX_SEATS:
                errors.append(f'seat_count must be a whole number from 1 to {MAX_SEATS}')
            else:
                core['seat_count'] = seats
        if errors:
            mysql.connection.rollback()
            cur.close()
            return _error('Validation failed', 400, details=errors)

        core = {k: val for k, val in core.items() if val != v[k]}                       # only real changes
        if core:
            cur.execute("""SELECT id, status FROM trips WHERE vehicle_id = %s AND status IN ('scheduled','moving','paused')
                           ORDER BY scheduled_departure LIMIT 1""", (vehicle_id,))
            busy = cur.fetchone()
            if busy:
                mysql.connection.rollback()
                cur.close()
                return _error(f"This vehicle has a {busy['status']} trip (trip #{busy['id']}), so its name, plate number and "
                              f"seats can't be changed now. Change them after the trip is finished or removed.", 409,
                              trip_id=busy['id'], status=busy['status'])
            if 'plate_number' in core:
                cur.execute("SELECT model_name FROM vehicles WHERE plate_number = %s AND id <> %s", (core['plate_number'], vehicle_id))
                other = cur.fetchone()
                if other:
                    mysql.connection.rollback()
                    cur.close()
                    return _error(f"Plate number {core['plate_number']} is already registered to {other['model_name']}", 409)

        sets, params = [], []
        if 'model_name' in core:
            sets += ['model_name = %s', 'name = %s']; params += [core['model_name']] * 2           # the old columns stay in step
        if 'plate_number' in core:
            sets += ['plate_number = %s', 'plate = %s']; params += [core['plate_number']] * 2
        if 'seat_count' in core:
            sets.append('seat_count = %s'); params.append(core['seat_count'])
        if sets:
            try:
                cur.execute(f"UPDATE vehicles SET {', '.join(sets)} WHERE id = %s", (*params, vehicle_id))
                mysql.connection.commit()
            except MySQLdb.IntegrityError as e:
                mysql.connection.rollback()
                cur.close()
                if e.args and e.args[0] == DUP_KEY:
                    return _error('That plate number is already registered to another vehicle', 409)
                app.logger.exception('edit vehicle failed')
                return _error('Could not save the vehicle', 500)
        else:
            mysql.connection.rollback()                                                 # nothing changed: just release the lock
        row = admin_vehicle_rows(cur, vehicle_id)[0]
        cur.close()
        return jsonify(row)

    # ---------- ADMIN: drivers (a driver is chosen per trip; phones are only ever sent by these admin routes) ----------
    def driver_rows(cur, day=None, only_id=None, removed=False):
        """name, phone, status, trips_completed (arrived trips on `day`, or all-time) and last_trip (date of the most
        recent arrived trip, whatever `day` is)."""
        params = []
        completed = "(SELECT COUNT(*) FROM trips t WHERE t.driver_id = d.id AND t.status = 'arrived'"
        if day is not None:
            completed += " AND t.trip_date = %s"
            params.append(day)
        completed += ") AS trips_completed"
        where = " WHERE d.removed_at IS NOT NULL" if removed else " WHERE d.removed_at IS NULL"
        if only_id is not None:
            where = " WHERE d.id = %s"
            params.append(only_id)
        cur.execute(f"""SELECT d.id, d.full_name AS name, d.phone, d.status, d.removed_at, {completed},
                               (SELECT MAX(t.trip_date) FROM trips t WHERE t.driver_id = d.id AND t.status = 'arrived') AS last_trip
                        FROM drivers d{where} ORDER BY d.full_name, d.id""", params)
        return [_row(r) for r in cur.fetchall()]

    def driver_duplicate(cur, name, phone, exclude_id=None):
        cur.execute("SELECT id, removed_at FROM drivers WHERE full_name = %s AND phone <=> %s AND id <> %s LIMIT 1",
                    (name, phone, exclude_id or 0))
        return cur.fetchone()

    @app.route('/api/admin/drivers')
    @admin_required
    def admin_drivers():
        raw = request.args.get('date')
        day = None
        if raw:                                               # no date = all-time
            try:
                day = parse_date_arg(raw)
            except ValueError as e:
                return _error(str(e))
        cur = dict_cursor()
        rows = driver_rows(cur, day, removed=request.args.get('removed') in ('1', 'true'))
        cur.close()
        return jsonify(rows)

    @app.route('/api/admin/drivers', methods=['POST'])
    @admin_required
    def admin_add_driver():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return _error('Request body must be a JSON object')
        errors = []
        name = _driver_name(data.get('full_name'), errors)
        phone, phone_error = parse_phone(data.get('phone'))
        if phone_error:
            errors.append(phone_error)
        if errors:
            return _error('Validation failed', 400, details=errors)
        cur = dict_cursor()
        try:
            dup = driver_duplicate(cur, name, phone)
            if dup:
                cur.close()
                if dup['removed_at']:
                    return _error(f"A driver named {name}{' with phone ' + phone if phone else ''} was removed earlier. "
                                  f"Restore them on the Drivers page (Show removed, then Restore) instead of adding them again.", 409, removed=True)
                return _error(f"A driver named {name}{' with phone ' + phone if phone else ' (no phone)'} already exists", 409)
            cur.execute("INSERT INTO drivers (full_name, phone) VALUES (%s, %s)", (name, phone))
            new_id = cur.lastrowid
            mysql.connection.commit()
        except MySQLdb.IntegrityError:
            mysql.connection.rollback()
            cur.close()
            return _error(f"A driver named {name} with that phone already exists", 409)
        except Exception:
            mysql.connection.rollback()
            cur.close()
            app.logger.exception('add driver failed')
            return _error('Could not save the driver', 500)
        row = driver_rows(cur, None, new_id)[0]
        cur.close()
        return jsonify(row), 201

    @app.route('/api/admin/drivers/<int:driver_id>', methods=['PUT'])
    @admin_required
    def admin_edit_driver(driver_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return _error('Request body must be a JSON object')
        cur = dict_cursor()
        cur.execute("SELECT id, full_name, phone, status, removed_at FROM drivers WHERE id = %s FOR UPDATE", (driver_id,))
        d = cur.fetchone()
        if d is None:
            cur.close()
            return _error('Driver not found', 404)
        if d['removed_at']:
            mysql.connection.rollback()
            cur.close()
            return _error(f"{d['full_name']} was removed. Restore them first (Show removed, then Restore).", 409)
        errors = []
        name, phone, status = d['full_name'], d['phone'], d['status']
        if 'full_name' in data:
            name = _driver_name(data.get('full_name'), errors)
        if 'phone' in data:
            phone, phone_error = parse_phone(data.get('phone'))
            if phone_error:
                errors.append(phone_error)
        if 'status' in data:
            status = data.get('status')
            if status not in ('active', 'inactive'):
                errors.append("status must be 'active' or 'inactive'")
        if errors:
            mysql.connection.rollback()
            cur.close()
            return _error('Validation failed', 400, details=errors)

        try:
            if driver_duplicate(cur, name, phone, exclude_id=driver_id):
                mysql.connection.rollback()
                cur.close()
                return _error(f"Another driver (possibly a removed one) named {name}{' with phone ' + phone if phone else ' (no phone)'} already exists", 409)
            if status == 'inactive' and d['status'] == 'active':
                marks = ','.join(['%s'] * len(DRIVER_OPEN_STATUSES))
                cur.execute(f"""SELECT id, status, scheduled_departure FROM trips
                                WHERE driver_id = %s AND status IN ({marks}) ORDER BY scheduled_departure LIMIT 1""",
                            (driver_id, *DRIVER_OPEN_STATUSES))
                busy = cur.fetchone()
                if busy:
                    mysql.connection.rollback()
                    cur.close()
                    return _error(f"{d['full_name']} has a {busy['status']} trip (trip #{busy['id']}, "
                                  f"{busy['scheduled_departure']:%Y-%m-%d %H:%M}), so they can't be made inactive now. "
                                  f"Remove or finish that trip first.", 409, trip_id=busy['id'], status=busy['status'])
            cur.execute("UPDATE drivers SET full_name = %s, phone = %s, status = %s WHERE id = %s",
                        (name, phone, status, driver_id))
            if name != d['full_name']:       # trips still to run show the new spelling; finished trips keep the name they were driven under
                marks = ','.join(['%s'] * len(DRIVER_OPEN_STATUSES))
                cur.execute(f"UPDATE trips SET driver_name = %s WHERE driver_id = %s AND status IN ({marks})",
                            (name, driver_id, *DRIVER_OPEN_STATUSES))
            mysql.connection.commit()
        except MySQLdb.IntegrityError:
            mysql.connection.rollback()
            cur.close()
            return _error(f"Another driver named {name} with that phone already exists", 409)
        except Exception:
            mysql.connection.rollback()
            cur.close()
            app.logger.exception('edit driver failed')
            return _error('Could not save the driver', 500)
        row = driver_rows(cur, None, driver_id)[0]
        cur.close()
        return jsonify(row)


    # ---------- ADMIN: remove / restore a vehicle or a driver ----------
    def open_trip_of(cur, column, item_id):
        marks = ','.join(['%s'] * len(DRIVER_OPEN_STATUSES))
        cur.execute(f"""SELECT id, status, scheduled_departure FROM trips WHERE {column} = %s AND status IN ({marks})
                        ORDER BY scheduled_departure LIMIT 1 FOR UPDATE""", (item_id, *DRIVER_OPEN_STATUSES))
        return cur.fetchone()

    def removal(kind, item_id):
        """The shared rule. No open trip (409 naming it); no trips at all: delete for good; any past trip: set removed_at."""
        table, column = ('vehicles', 'vehicle_id') if kind == 'vehicle' else ('drivers', 'driver_id')
        cur = dict_cursor()
        if kind == 'vehicle':
            cur.execute("SELECT id, model_name, plate_number, removed_at FROM vehicles WHERE id = %s FOR UPDATE", (item_id,))
        else:
            cur.execute("SELECT id, full_name, removed_at FROM drivers WHERE id = %s FOR UPDATE", (item_id,))
        row = cur.fetchone()
        if row is None:
            cur.close()
            return _error(f"{kind.capitalize()} not found", 404)
        label = f"{row['model_name']} ({row['plate_number']})" if kind == 'vehicle' else row['full_name']
        try:
            if row['removed_at']:
                mysql.connection.rollback()
                return _error(f"{label} is already removed.", 409)
            busy = open_trip_of(cur, column, item_id)
            if busy:
                mysql.connection.rollback()
                return _error(f"{label} has a {busy['status']} trip (trip #{busy['id']}, {busy['scheduled_departure']:%Y-%m-%d %H:%M}). "
                              f"Cancel or finish trip #{busy['id']} first.", 409, trip_id=busy['id'], status=busy['status'])
            cur.execute(f"SELECT COUNT(*) AS n FROM trips WHERE {column} = %s", (item_id,))
            if cur.fetchone()['n'] == 0:
                if kind == 'vehicle':                              # its own GPS history goes with it (locations has no foreign key)
                    cur.execute("DELETE FROM locations WHERE vehicle_id = %s", (item_id,))
                cur.execute(f"DELETE FROM {table} WHERE id = %s", (item_id,))
                mysql.connection.commit()
                return jsonify({'result': 'deleted', 'id': item_id, 'name': label,
                                'message': f"{label} had no trips, so it was deleted permanently."})
            cur.execute(f"UPDATE {table} SET removed_at = NOW() WHERE id = %s", (item_id,))
            mysql.connection.commit()
            return jsonify({'result': 'removed', 'id': item_id, 'name': label,
                            'message': f"{label} removed; kept for past reports."})
        except Exception:
            mysql.connection.rollback()
            app.logger.exception('remove %s failed', kind)
            return _error(f'Could not remove the {kind}', 500)
        finally:
            cur.close()

    def restoration(kind, item_id):
        table = 'vehicles' if kind == 'vehicle' else 'drivers'
        cur = dict_cursor()
        if kind == 'vehicle':
            cur.execute("SELECT id, model_name, plate_number, removed_at FROM vehicles WHERE id = %s FOR UPDATE", (item_id,))
        else:
            cur.execute("SELECT id, full_name, phone, removed_at FROM drivers WHERE id = %s FOR UPDATE", (item_id,))
        row = cur.fetchone()
        if row is None:
            cur.close()
            return _error(f"{kind.capitalize()} not found", 404)
        label = f"{row['model_name']} ({row['plate_number']})" if kind == 'vehicle' else row['full_name']
        try:
            if not row['removed_at']:
                mysql.connection.rollback()
                return _error(f"{label} is not removed.", 409)
            if kind == 'vehicle':
                cur.execute("SELECT model_name FROM vehicles WHERE plate_number = %s AND id <> %s AND removed_at IS NULL",
                            (row['plate_number'], item_id))
                other = cur.fetchone()
                if other:
                    mysql.connection.rollback()
                    return _error(f"Plate number {row['plate_number']} is now used by {other['model_name']}, so {label} can't be restored.", 409)
            else:
                cur.execute("SELECT id FROM drivers WHERE full_name = %s AND phone <=> %s AND id <> %s AND removed_at IS NULL",
                            (row['full_name'], row['phone'], item_id))
                if cur.fetchone():
                    mysql.connection.rollback()
                    return _error(f"Another driver named {label} with the same phone already exists, so this one can't be restored.", 409)
            cur.execute(f"UPDATE {table} SET removed_at = NULL WHERE id = %s", (item_id,))
            mysql.connection.commit()
            out = (admin_vehicle_rows(cur, item_id) if kind == 'vehicle' else driver_rows(cur, None, item_id))[0]
            out['message'] = f"{label} restored."
            return jsonify(out)
        except Exception:
            mysql.connection.rollback()
            app.logger.exception('restore %s failed', kind)
            return _error(f'Could not restore the {kind}', 500)
        finally:
            cur.close()

    @app.route('/api/admin/vehicles/<int:vehicle_id>', methods=['DELETE'])
    @admin_required
    def admin_remove_vehicle(vehicle_id):
        return removal('vehicle', vehicle_id)

    @app.route('/api/admin/drivers/<int:driver_id>', methods=['DELETE'])
    @admin_required
    def admin_remove_driver(driver_id):
        return removal('driver', driver_id)

    @app.route('/api/admin/vehicles/<int:vehicle_id>/restore', methods=['POST'])
    @admin_required
    def admin_restore_vehicle(vehicle_id):
        return restoration('vehicle', vehicle_id)

    @app.route('/api/admin/drivers/<int:driver_id>/restore', methods=['POST'])
    @admin_required
    def admin_restore_driver(driver_id):
        return restoration('driver', driver_id)

    # ---------- ADMIN: monthly / yearly report PDFs ----------
    def period_pdf(kind, param):
        try:
            start, end, label = parse_period(kind, request.args.get(param))
        except ValueError as e:
            return _error(str(e))
        cur = dict_cursor()
        data = period_report_data(cur, start, end, 'day' if kind == 'month' else 'month')
        cur.close()
        if data is None:
            return _error(f'No trips found for {label}', 404)
        try:
            pdf = build_period_pdf(kind, label, data)
        except Exception:
            app.logger.exception('period PDF failed')
            return _error('Could not build the PDF', 500)
        name = (f"{VIEWER_FILE_PREFIX}_Monthly_Report_{start:%Y-%m}.pdf" if kind == 'month'
                else f"{VIEWER_FILE_PREFIX}_Yearly_Report_{start.year}.pdf")
        return _pdf_response(pdf, name)

    @app.route('/api/admin/reports/monthly.pdf')
    @admin_required
    def monthly_report_pdf():
        return period_pdf('month', 'month')

    @app.route('/api/admin/reports/yearly.pdf')
    @admin_required
    def yearly_report_pdf():
        return period_pdf('year', 'year')

    @app.route('/api/admin/reports/years')
    @admin_required
    def report_years():
        cur = dict_cursor()
        cur.execute("SELECT MIN(YEAR(trip_date)) AS first FROM trips WHERE status <> 'cancelled'")
        first = cur.fetchone()['first']
        cur.close()
        this_year = date.today().year
        return jsonify({'years': list(range(this_year, min(first or this_year, this_year) - 1, -1))})

    # ---------- KPIs ----------
    @app.route('/api/kpis')
    @login_required
    def kpis():
        try:
            day = day_arg()
        except ValueError as e:
            return _error(str(e))
        cur = dict_cursor()
        result = compute_kpis(cur, day)
        cur.close()
        return jsonify(result)
