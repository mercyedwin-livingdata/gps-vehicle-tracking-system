from flask import Flask, jsonify, request, send_from_directory
from flask_mysqldb import MySQL
from flask_cors import CORS
from datetime import datetime
from ai_module import predict_traffic, suggest_route
import config
import MySQLdb.cursors
from trips import register_trip_routes, trip_status_summary, parse_date_arg, active_vehicles, ensure_vehicle_columns, ensure_driver_schema
from simulator import run_simulator
from auth import admin_required, configure_auth, login_required, register_auth_routes
import os
import socket
import threading

app = Flask(__name__)
# The login cookie only travels to pages served from this machine (localhost / 127.0.0.1, any port)
CORS(app, supports_credentials=True, origins=[r'^http://(localhost|127\.0\.0\.1)(:\d+)?$'])
configure_auth(app)

simulator_thread = None
simulator_lock = threading.Lock()

def start_simulator_thread():
    global simulator_thread
    with simulator_lock:
        if simulator_thread is None or not simulator_thread.is_alive():
            simulator_thread = threading.Thread(target=run_simulator, daemon=True)
            simulator_thread.start()

# ── DATABASE CONFIG ──────────────────────────────
app.config['MYSQL_HOST']     = 'localhost'
app.config['MYSQL_USER']     = 'root'
app.config['MYSQL_PASSWORD'] = ''
app.config['MYSQL_DB']       = 'gps_tracking'

mysql = MySQL(app)
register_trip_routes(app, mysql)
init_accounts = register_auth_routes(app, mysql)      # /api/auth/register|login|logout|me + the users table


# ── THE DASHBOARD PAGE (served from here so the login cookie is same-origin: open http://localhost:5000) ──
@app.route('/')
def dashboard_page():
    page = send_from_directory(app.root_path, 'index.html')
    page.headers['Cache-Control'] = 'no-store'
    return page


# ── SEED THE COMPANY FLEET (safe to run any time) ─
# INSERT-ONLY: a vehicle whose plate is already in the table is skipped, never updated, so running this
# again can't reset fuel, position or drivers. It writes to `vehicles` only - trips, passengers,
# trip_events and locations are never touched.
@app.route('/seed')
@admin_required
def seed():
    cur = mysql.connection.cursor()
    inserted, skipped = [], []

    for v in config.FLEET:
        cur.execute("SELECT 1 FROM vehicles WHERE plate_number = %s", (v['plate'],))
        if cur.fetchone():
            skipped.append(v['plate'])
            continue
        start = config.TOWNS[v['start']]
        cur.execute("SELECT 1 FROM vehicles WHERE id = %s", (v['id'],))
        id_taken = cur.fetchone() is not None          # keep the configured id when it is free, else let MySQL pick one
        cur.execute(f"""
            INSERT INTO vehicles
            ({'' if id_taken else 'id, '}name, driver, plate, status, speed, fuel, latitude, longitude, trips,
             plate_number, model_name, seat_count, driver_name)
            VALUES ({'' if id_taken else '%s,'}%s,%s,%s,'idle',0,100,%s,%s,0,%s,%s,%s,%s)
        """, (() if id_taken else (v['id'],)) +
             (v['name'], v['driver'], v['plate'], start['lat'], start['lng'],
              v['plate'], v['name'], v['seats'], v['driver']))
        inserted.append(v['plate'])

    mysql.connection.commit()
    cur.close()
    return jsonify({'message': 'Fleet checked. Existing vehicles were left exactly as they were.',
                    'inserted': inserted, 'already_registered': skipped})


# ── PUBLIC INFO (no login: just enough for the login screen) ──
@app.route('/api/public/info')
def public_info():
    return jsonify({'company': config.COMPANY_NAME})


# ── APP CONFIG (company name + map centre for the dashboard) ──
@app.route('/api/config')
@login_required
def get_config():
    return jsonify({
        'company_name': config.COMPANY_NAME,
        'map_center':   config.MAP_CENTER,
        'towns':        config.TOWNS,
    })


# ── VEHICLES ON THE ROAD ─────────────────────────
# Only vehicles with a moving / paused / stopped trip on ?date= (default today) are returned.
@app.route('/api/vehicles')
@login_required
def get_vehicles():
    try:
        day = parse_date_arg(request.args.get('date'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    vehicles = active_vehicles(cur, day)
    cur.close()
    return jsonify(vehicles)


# ── GET ONE VEHICLE ──────────────────────────────
@app.route('/api/vehicles/<int:vehicle_id>')
@login_required
def get_vehicle(vehicle_id):
    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM vehicles WHERE id = %s", (vehicle_id,))
    row = cur.fetchone()
    cols = [c[0] for c in cur.description]
    cur.close()

    if row is None or ('removed_at' in cols and row[cols.index('removed_at')] is not None):
        return jsonify({'error': 'Vehicle not found'}), 404

    return jsonify({
        'id':        row[0],
        'name':      row[1],
        'driver':    row[2],
        'plate':     row[3],
        'status':    row[4],
        'speed':     row[5],
        'fuel':      row[6],
        'latitude':  row[7],
        'longitude': row[8],
        'route':     row[9],
        'eta':       row[10],
        'trips':     row[11],
    })


# ── UPDATE VEHICLE LOCATION ──────────────────────
@app.route('/api/vehicles/<int:vehicle_id>/location', methods=['POST'])
@admin_required
def update_location(vehicle_id):
    data = request.get_json()
    lat   = data.get('latitude')
    lng   = data.get('longitude')
    speed = data.get('speed', 0)

    cur = mysql.connection.cursor()

    # update main vehicle table
    cur.execute("""
        UPDATE vehicles
        SET latitude=%s, longitude=%s, speed=%s
        WHERE id=%s
    """, (lat, lng, speed, vehicle_id))

    # save to location history
    cur.execute("""
        INSERT INTO locations (vehicle_id, latitude, longitude, speed)
        VALUES (%s, %s, %s, %s)
    """, (vehicle_id, lat, lng, speed))

    mysql.connection.commit()
    cur.close()
    return jsonify({'message': 'Location updated'})


@app.route('/api/vehicles/<int:vehicle_id>/refuel', methods=['POST'])
@admin_required
def refuel_vehicle(vehicle_id):
    """Refuel a vehicle and optionally start it moving again."""
    data = request.get_json() or {}
    amount = data.get('amount', 50)
    start   = bool(data.get('start', False))

    # sanitize
    try:
        amount = float(amount)
    except Exception:
        amount = 50
    amount = max(0, min(100, amount))

    status = 'moving' if start else 'idle'

    cur = mysql.connection.cursor()
    cur.execute("""
        UPDATE vehicles
        SET fuel = LEAST(100, GREATEST(0, fuel + %s)), status = %s
        WHERE id = %s
    """, (amount, status, vehicle_id))
    mysql.connection.commit()

    # rowcount is 0 when nothing changed (e.g. already at 100), so confirm existence by reading back
    cur.execute("SELECT fuel FROM vehicles WHERE id = %s", (vehicle_id,))
    row = cur.fetchone()
    cur.close()
    if row is None:
        return jsonify({'error': 'Vehicle not found'}), 404

    return jsonify({'message': 'Vehicle refueled', 'vehicle_id': vehicle_id, 'fuel': round(float(row[0]), 1), 'status': status})


# ── GET LOCATION HISTORY ─────────────────────────
@app.route('/api/vehicles/<int:vehicle_id>/history')
@login_required
def get_history(vehicle_id):
    cur = mysql.connection.cursor()
    cur.execute("""
        SELECT latitude, longitude, speed, timestamp
        FROM locations
        WHERE vehicle_id = %s
        ORDER BY timestamp DESC
        LIMIT 20
    """, (vehicle_id,))
    rows = cur.fetchall()
    cur.close()

    history = []
    for row in rows:
        history.append({
            'latitude':  row[0],
            'longitude': row[1],
            'speed':     row[2],
            'timestamp': str(row[3]),
        })
    return jsonify(history)


# ── AI TRAFFIC PREDICTION ────────────────────────
@app.route('/api/ai/predict/<int:vehicle_id>')
@login_required
def predict_traffic_route(vehicle_id):
    cur = mysql.connection.cursor()

    # Get vehicle data
    cur.execute("SELECT * FROM vehicles WHERE id = %s", (vehicle_id,))
    row = cur.fetchone()
    if row is None:
        return jsonify({'error': 'Vehicle not found'}), 404

    vehicle_data = {
        'id':     row[0], 'name':  row[1],
        'speed':  row[5], 'fuel':  row[6],
        'route':  row[9],
    }

    # Get location history
    cur.execute("""
        SELECT speed, timestamp FROM locations
        WHERE vehicle_id = %s
        ORDER BY timestamp DESC LIMIT 10
    """, (vehicle_id,))
    history_rows = cur.fetchall()
    cur.close()

    location_history = [
        {'speed': r[0], 'timestamp': str(r[1])}
        for r in history_rows
    ]

    # Run Scikit-learn AI prediction
    result     = predict_traffic(vehicle_data, location_history)
    route_info = suggest_route(vehicle_data, result['risk'])

    return jsonify({
        'vehicle_id':   vehicle_id,
        'prediction':   result['prediction'],
        'suggestion':   result['suggestion'],
        'risk':         result['risk'],
        'confidence':   result['confidence'],
        'avg_speed':    result['avg_speed'],
        'rush_hour':    result['rush_hour'],
        'route_option': route_info,
        'timestamp':    str(datetime.now()),
    })

    # simple AI logic using average speed
    speeds = [row[2] for row in rows]
    avg_speed = sum(speeds) / len(speeds)

    hour = datetime.now().hour
    is_rush_hour = (7 <= hour <= 9) or (16 <= hour <= 19)

    if avg_speed < 20 and is_rush_hour:
        prediction = 'Heavy traffic detected on current route'
        suggestion = 'Use alternative route to save time'
        risk       = 'high'
    elif avg_speed < 35:
        prediction = 'Moderate congestion detected'
        suggestion = 'Monitor speed — possible delays ahead'
        risk       = 'medium'
    else:
        prediction = 'Route is clear — good conditions'
        suggestion = 'Continue on current route'
        risk       = 'low'

    return jsonify({
        'vehicle_id':  vehicle_id,
        'avg_speed':   round(avg_speed, 1),
        'rush_hour':   is_rush_hour,
        'prediction':  prediction,
        'suggestion':  suggestion,
        'risk':        risk,
        'timestamp':   str(datetime.now())
    })


# ── FLEET SUMMARY (trips on the selected date only) ──
@app.route('/api/fleet/summary')
@login_required
def fleet_summary():
    try:
        day = parse_date_arg(request.args.get('date'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    summary = trip_status_summary(cur, day)
    cur.close()
    return jsonify(summary)


# ── HEALTH (no login; tells "Flask is up" apart from "MySQL is up") ──
@app.route('/api/health')
def health():
    try:
        # A refused connection takes 2-4 s to fail on Windows (it retries each address), longer than the page waits.
        # A short TCP probe of the MySQL port fails fast; only if it answers do we run the real query.
        host = app.config.get('MYSQL_HOST', 'localhost')
        socket.create_connection(('127.0.0.1' if host == 'localhost' else host, app.config.get('MYSQL_PORT', 3306)),
                                 timeout=0.8).close()
        cur = mysql.connection.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        cur.close()
        return jsonify({'server': 'ok', 'database': 'ok'})
    except Exception:
        return jsonify({'server': 'ok', 'database': 'down'})


def init_vehicle_columns():
    """Startup: add vehicles.driver_phone if it is missing (the first request that needs it retries if MySQL was down now)."""
    try:
        with app.app_context():
            cur = mysql.connection.cursor()
            ensure_vehicle_columns(cur)
            mysql.connection.commit()
            cur.close()
            ensure_driver_schema(mysql.connection)       # drivers table, trips.driver_id, one-time migration
    except Exception as e:
        print(f'WARNING: could not check the vehicles/drivers tables yet ({type(e).__name__}); will retry on the first request that needs it.')


# ── RUN SERVER ───────────────────────────────────
if __name__ == '__main__':
    DEBUG = True  # set False to run without the reloader; the simulator starts either way
    # With the debug reloader this file runs twice; only the child has WERKZEUG_RUN_MAIN set
    if not DEBUG or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        init_accounts()
        init_vehicle_columns()          # tables and columns first: the simulator's queries read vehicles.removed_at
        start_simulator_thread()
    app.run(debug=DEBUG, port=5000)
