"""Vehicle simulator, started by app.py in a background thread (python app.py is the only command).

Every TICK_SECONDS it:
  1. AUTO-STARTS 'scheduled' trips whose scheduled_departure has passed (today's trips only), using
     trips.begin_trip() - the same guarded logic as POST /api/trips/<id>/start, including the
     one-active-trip-per-vehicle rule. A busy vehicle leaves the trip scheduled; it is retried next tick.
  2. MOVES every 'moving' trip from where it is toward the destination town. Speed is the one that
     covers the straight-line distance in the estimated duration (MIN_KMH..MAX_KMH), with jitter on
     every tick. About SLOWDOWN_CHANCE of trips get one slowdown (30-60% of normal speed for a stretch)
     with a random reason; on a late arrival that reason becomes delay_reason (unless one is already set).
  3. DROP-OFF STOPS: within STOP_RADIUS_KM of (or just past) a passenger's drop-off town the vehicle
     alights those passengers with trips.alight_passenger() - the same logic as POST .../alight - and holds
     for STOP_TICKS ticks. Drop-off towns more than OFF_ROUTE_KM from the A-B line (or unknown) are served
     at the destination instead, with the reason in the event note. Passengers already dropped are skipped.
  4. ARRIVES with trips.mark_arrived() - the same logic as POST .../arrive.

Paused / stopped / scheduled trips are never moved. SIM_SPEEDUP=60 makes simulated time pass 60x faster.
SIM_SLOWDOWN_CHANCE overrides the 30% slowdown chance (handy for testing).
"""
import math
import os
import random
import time
from datetime import datetime

import mysql.connector

import config
from trips import alight_passenger, begin_trip, mark_arrived

TICK_SECONDS = 5
SIM_SPEEDUP = float(os.environ.get('SIM_SPEEDUP', '1'))
SLOWDOWN_CHANCE = float(os.environ.get('SIM_SLOWDOWN_CHANCE', '0.3'))
MIN_KMH, MAX_KMH = 5, 90
STOP_TICKS = 2          # ticks the vehicle stands still after letting passengers off
STOP_RADIUS_KM = 1.5    # "reached the drop-off town"
OFF_ROUTE_KM = 5.0      # a drop-off town farther than this from the A-B line is served at the destination
DELAY_REASONS = ['Traffic congestion', 'Road works', 'Bad road', 'Police checkpoint', 'Heavy rain']
TOWN_BY_KEY = {name.lower(): name for name in config.TOWNS}

_state = {}             # trip_id -> slowdown plan and stop bookkeeping (in memory; re-rolled after a restart)


def log(msg):
    print(msg, flush=True)


def get_connection():
    return mysql.connector.connect(host='localhost', user='root', password='', database='gps_tracking')


# ── GEOMETRY ─────────────────────────────────────
def distance_km(lat1, lng1, lat2, lng2):
    """Straight-line distance, accurate enough at this scale."""
    dy = (lat2 - lat1) * 110.57
    dx = (lng2 - lng1) * 111.32 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dx, dy)


def route_position(o, d, town):
    """Where a town sits relative to the straight line A->B: (t, off_km).
    t = 0..1 is the point of the route closest to the town; off_km is the town's distance from the line."""
    k = math.cos(math.radians(o['lat']))
    ax, ay = o['lng'] * 111.32 * k, o['lat'] * 110.57
    bx, by = d['lng'] * 111.32 * k, d['lat'] * 110.57
    px, py = town['lng'] * 111.32 * k, town['lat'] * 110.57
    vx, vy = bx - ax, by - ay
    length2 = vx * vx + vy * vy
    t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / length2)) if length2 else 0.0
    return t, math.hypot(px - (ax + t * vx), py - (ay + t * vy))


def nearest_town(lat, lng):
    return min(config.TOWNS, key=lambda n: distance_km(lat, lng, config.TOWNS[n]['lat'], config.TOWNS[n]['lng']))


# ── AUTO-START ───────────────────────────────────
def auto_start_due_trips(conn, cur, now):
    cur.execute("""
        SELECT t.id, t.vehicle_id, t.origin, t.destination, COALESCE(v.model_name, v.name) AS name
        FROM trips t JOIN vehicles v ON v.id = t.vehicle_id
        WHERE t.status = 'scheduled' AND t.trip_date = %s AND t.scheduled_departure <= %s AND v.removed_at IS NULL
        ORDER BY t.scheduled_departure, t.id
    """, (now.date(), now))
    for t in cur.fetchall():
        try:
            result, other = begin_trip(cur, t['id'], t['vehicle_id'], t['origin'], t['destination'], now,
                                       note='Auto-started at scheduled departure')
            if result == 'started':
                conn.commit()
                log(f"  [auto-start] {t['name']} trip {t['id']} {t['origin']} -> {t['destination']}")
            else:
                conn.rollback()
                if result == 'busy':
                    log(f"  [auto-start] trip {t['id']} waits: vehicle still on trip {other}")
        except Exception as e:
            conn.rollback()
            log(f"  Auto-start failed for trip {t['id']}: {e}")


# ── SLOWDOWN ─────────────────────────────────────
def new_state():
    plan = None
    if random.random() < SLOWDOWN_CHANCE:
        start = random.uniform(0.10, 0.70)
        plan = {'start': start, 'end': start + random.uniform(0.10, 0.30),
                'factor': random.uniform(0.30, 0.60), 'reason': random.choice(DELAY_REASONS)}
    return {'plan': plan, 'active': False, 'done': False, 'reason': None, 'hold': 0, 'stop_at': ''}


def slowdown_factor(cur, trip_id, st, progress, lat, lng, now):
    """1.0 normally; the plan's factor during the slowdown stretch. The first slow tick writes the event."""
    plan = st['plan']
    if plan is None or st['done']:
        return 1.0
    if st['active'] and progress >= plan['end']:
        st['active'], st['done'] = False, True
        return 1.0
    if not st['active'] and progress >= plan['start']:
        st['active'], st['reason'] = True, plan['reason']
        cur.execute("""INSERT INTO trip_events (trip_id, event_type, location, event_time, note)
                       VALUES (%s,'slowdown',%s,%s,%s)""",
                    (trip_id, f"near {nearest_town(lat, lng)}", now,
                     f"{plan['reason']}: speed cut to {round(plan['factor'] * 100)}% of normal for part of the route"))
        log(f"  [slowdown] trip {trip_id}: {plan['reason']} (x{plan['factor']:.2f})")
    return plan['factor'] if st['active'] else 1.0


# ── DROP-OFFS ────────────────────────────────────
def waiting_passengers(cur, trip_id):
    cur.execute("""SELECT id, passenger_name, seat_label, drop_off_location FROM passengers
                   WHERE trip_id = %s AND alighted_at IS NULL ORDER BY id""", (trip_id,))
    return cur.fetchall()


def drop_at_reached_towns(cur, trip, o, d, lat, lng, progress, st, now):
    """Alight passengers whose drop-off town has been reached; start the hold. Returns log lines."""
    lines, stops = [], {}
    for p in waiting_passengers(cur, trip['id']):
        town = TOWN_BY_KEY.get((p['drop_off_location'] or '').strip().lower())
        if town is None or town in (trip['origin'], trip['destination']):
            continue                                    # served at the destination (see arrive_trip)
        c = config.TOWNS[town]
        t, off_km = route_position(o, d, c)
        if off_km > OFF_ROUTE_KM:
            continue                                    # too far from the route: destination instead
        if distance_km(lat, lng, c['lat'], c['lng']) <= STOP_RADIUS_KM or progress >= t:
            stops.setdefault(town, []).append(p)
    for town, people in stops.items():
        for p in people:
            if alight_passenger(cur, trip['id'], p['id'], town, now):
                lines.append(f"  [alight] {p['passenger_name']} ({p['seat_label']}) got off at {town}")
    if stops:
        st['hold'] = STOP_TICKS
        st['stop_at'] = ', '.join(stops)
    return lines


def drop_remaining_at_destination(cur, trip, o, d, now):
    """On arrival everyone still on board gets off at the destination; the note says why if it wasn't their stop."""
    lines = []
    for p in waiting_passengers(cur, trip['id']):
        place = (p['drop_off_location'] or '').strip()
        town = TOWN_BY_KEY.get(place.lower())
        note = None
        if town == trip['destination']:
            pass
        elif town is None:
            note = f"drop-off '{place}' is not a known town; dropped at the destination"
        elif town == trip['origin']:
            note = f"drop-off {town} is the starting town; dropped at the destination"
        else:
            _, off_km = route_position(o, d, config.TOWNS[town])
            note = (f"drop-off {town} is {off_km:.1f} km from the {trip['origin']}-{trip['destination']} route "
                    f"(limit {OFF_ROUTE_KM:g} km); dropped at the destination instead") if off_km > OFF_ROUTE_KM \
                else f"reached the destination before the {town} stop; dropped at the destination"
        if alight_passenger(cur, trip['id'], p['id'], trip['destination'], now, note=note):
            lines.append(f"  [alight] {p['passenger_name']} ({p['seat_label']}) got off at {trip['destination']}"
                         + (f"  <- {note}" if note else ""))
    return lines


# ── ONE TICK FOR ONE MOVING TRIP ─────────────────
def move_trip(cur, trip, now):
    tid, vid = trip['id'], trip['vehicle_id']
    st = _state.setdefault(tid, new_state())
    o, d = config.TOWNS.get(trip['origin']), config.TOWNS.get(trip['destination'])
    if o is None or d is None:
        return [f"  trip {tid}: unknown town ({trip['origin']} / {trip['destination']}), skipped"]
    lat = trip['latitude'] if trip['latitude'] is not None else o['lat']
    lng = trip['longitude'] if trip['longitude'] is not None else o['lng']

    if st['hold'] > 0:                                  # standing at a drop-off stop
        st['hold'] -= 1
        cur.execute("UPDATE vehicles SET speed = 0 WHERE id = %s", (vid,))
        cur.execute("INSERT INTO locations (vehicle_id, latitude, longitude, speed) VALUES (%s,%s,%s,0)",
                    (vid, lat, lng))
        return [f"  trip {tid}: stopped at {st['stop_at']} ({st['hold']} tick(s) left)"]

    trip_km = distance_km(o['lat'], o['lng'], d['lat'], d['lng'])
    remaining = distance_km(lat, lng, d['lat'], d['lng'])
    progress = max(0.0, min(1.0, 1 - remaining / trip_km)) if trip_km else 1.0
    factor = slowdown_factor(cur, tid, st, progress, lat, lng, now)

    base_kmh = min(MAX_KMH, max(MIN_KMH, trip_km / max(trip['estimated_duration_minutes'], 1) * 60))
    speed = max(2.0, base_kmh * random.uniform(0.9, 1.1) * factor)          # varies a little every tick
    step_km = speed * TICK_SECONDS * SIM_SPEEDUP / 3600

    if remaining <= step_km:                                                 # arrived
        cur.execute("UPDATE vehicles SET latitude = %s, longitude = %s WHERE id = %s", (d['lat'], d['lng'], vid))
        cur.execute("INSERT INTO locations (vehicle_id, latitude, longitude, speed) VALUES (%s,%s,%s,0)",
                    (vid, d['lat'], d['lng']))
        lines = drop_remaining_at_destination(cur, trip, o, d, now)
        ok = mark_arrived(cur, tid, vid, trip['destination'], now, fallback_reason=st['reason'])
        _state.pop(tid, None)
        lines.append(f"  trip {tid}: ARRIVED at {trip['destination']}" if ok
                     else f"  trip {tid}: reached {trip['destination']} but was no longer arrivable")
        return lines

    ratio = step_km / remaining
    new_lat, new_lng = lat + (d['lat'] - lat) * ratio, lng + (d['lng'] - lng) * ratio
    eta_min = math.ceil((remaining - step_km) / (speed * SIM_SPEEDUP) * 60)
    cur.execute("""UPDATE vehicles SET latitude = %s, longitude = %s, speed = %s, status = 'moving',
                       eta = %s, fuel = GREATEST(0, fuel - %s) WHERE id = %s""",
                (round(new_lat, 6), round(new_lng, 6), round(speed, 1), f"{eta_min} min",
                 random.uniform(0.1, 0.3), vid))
    cur.execute("INSERT INTO locations (vehicle_id, latitude, longitude, speed) VALUES (%s,%s,%s,%s)",
                (vid, round(new_lat, 6), round(new_lng, 6), round(speed, 1)))
    lines = [f"  trip {tid}: {trip['origin']} -> {trip['destination']} | {speed:5.1f} km/h"
             f"{' (slowdown)' if st['active'] else ''} | {remaining - step_km:5.1f} km to go"]
    lines += drop_at_reached_towns(cur, trip, o, d, new_lat, new_lng, 1 - (remaining - step_km) / trip_km, st, now)
    return lines


def tick():
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True, buffered=True)
        now = datetime.now().replace(microsecond=0)
        auto_start_due_trips(conn, cur, now)
        cur.execute("""
            SELECT t.id, t.vehicle_id, t.origin, t.destination, t.estimated_duration_minutes,
                   v.latitude, v.longitude, COALESCE(v.model_name, v.name) AS name
            FROM trips t JOIN vehicles v ON v.id = t.vehicle_id
            WHERE t.status = 'moving' AND v.removed_at IS NULL
        """)
        moving = cur.fetchall()
        for trip in moving:
            try:
                lines = move_trip(cur, trip, now)
                conn.commit()
                for line in lines:
                    log(line)
            except Exception as e:
                conn.rollback()
                log(f"  Database error for trip {trip['id']}: {e}")
        return len(moving)
    finally:
        conn.close()


def run_simulator():
    """Main loop: runs forever (daemon thread inside app.py, or foreground via python simulator.py)."""
    log("=" * 50)
    log("  GPS VEHICLE SIMULATOR STARTED")
    log(f"  Tick every {TICK_SECONDS}s, speed-up x{SIM_SPEEDUP:g}, slowdown chance {SLOWDOWN_CHANCE:.0%}")
    log("=" * 50)
    try:
        get_connection().close()
        log("  Database connected successfully\n")
    except Exception as e:
        log(f"  Cannot connect to database: {e}")
        log("  Make sure XAMPP MySQL is running!")
        return

    while True:
        try:
            tick()
        except Exception as e:
            log(f"  Simulator tick failed: {e}")
        time.sleep(TICK_SECONDS)


if __name__ == '__main__':
    run_simulator()
