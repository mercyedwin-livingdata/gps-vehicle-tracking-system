"""User accounts: the `users` table, register / login / logout / me, and the access rules.

Passwords are only ever stored as werkzeug hashes. The login state is Flask's signed session cookie
(HttpOnly, SameSite=Lax). Routes are protected with @login_required (any approved user) and @admin_required;
both look the user up in the database on every request, so a user who is rejected later loses access at once.
Viewers get trip data without passenger details (strip_for_viewer).
"""
import functools
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path

import MySQLdb
import MySQLdb.cursors
from dotenv import dotenv_values
from flask import g, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

ENV_PATH = Path(__file__).resolve().parent / '.env'

CREATE_USERS_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    full_name     VARCHAR(100) NOT NULL,
    email         VARCHAR(120) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role          ENUM('admin','viewer') NOT NULL DEFAULT 'viewer',
    status        ENUM('pending','approved','rejected') NOT NULL DEFAULT 'pending',
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    approved_at   DATETIME NULL
)"""

EMAIL_RE = re.compile(r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$')
MIN_PASSWORD, MAX_PASSWORD = 8, 128
MAX_FAILED, FAIL_WINDOW = 5, 60                      # 5 failed logins per minute per IP
DUMMY_HASH = generate_password_hash('not-a-real-password')   # checked for unknown emails so timing doesn't give them away


def _clean(value):
    """Trim spaces and one pair of surrounding quotes: ' "abc" ' -> abc."""
    value = (value or '').strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
        value = value[1:-1].strip()
    return value


def env_value(name):
    """One mechanism for SECRET_KEY, ADMIN_NAME, ADMIN_EMAIL and ADMIN_PASSWORD: a process environment variable wins,
    then the .env file, which is read fresh on every call (no restart needed to pick up an edit). Never printed."""
    value = _clean(os.environ.get(name))
    if value:
        return value
    try:
        from_file = dotenv_values(ENV_PATH, encoding='utf-8-sig') if ENV_PATH.exists() else {}   # utf-8-sig: Notepad's BOM is harmless
    except OSError:
        from_file = {}
    return _clean(from_file.get(name))


def configure_auth(app):
    key = env_value('SECRET_KEY')
    if not key:
        key = secrets.token_hex(32)
        print('WARNING: SECRET_KEY is not set in .env - using a temporary key, so everyone is logged out '
              'whenever the server restarts.')
    app.config.update(
        SECRET_KEY=key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )


def _error(message, code=400):
    return jsonify({'error': message}), code


# ── ACCESS RULES ─────────────────────────────────
_db = {'mysql': None}                                # set by register_auth_routes


def current_user():
    """The logged-in user if the session is valid AND the database still says 'approved' (checked on every request,
    once per request), else None. A rejected / deleted user's cookie stops working immediately."""
    if 'auth_user' in g:
        return g.auth_user
    user = None
    uid = session.get('uid')
    if uid:
        cur = _db['mysql'].connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT id, full_name, email, role, status FROM users WHERE id = %s", (uid,))
        row = cur.fetchone()
        cur.close()
        if row and row['status'] == 'approved':
            user = row
        else:
            session.clear()
    g.auth_user = user
    return user


def _guard(view, admin_only):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        try:
            user = current_user()
        except MySQLdb.Error:
            return _error('The database is offline. Start MySQL (XAMPP) and try again.', 503)
        if user is None:
            return _error('Not logged in', 401)
        if admin_only and user['role'] != 'admin':
            return _error('Admin only', 403)
        return view(*args, **kwargs)
    wrapper.access = 'admin' if admin_only else 'login'      # lets a test list every route's rule
    return wrapper


def login_required(view):
    """Any logged-in, approved user (admin or viewer). 401 JSON otherwise."""
    return _guard(view, False)


def admin_required(view):
    """Admins only: 401 JSON if not logged in, 403 JSON "Admin only" for a viewer."""
    return _guard(view, True)


def is_viewer():
    user = current_user()
    return user is not None and user['role'] != 'admin'


def strip_for_viewer(trips):
    """Viewers see counts only: no passenger names or details, and no event log (its notes can name passengers).
    passengers_total / _alighted / _onboard stay."""
    for t in trips:
        t['passengers'] = []
        t.pop('events', None)
    return trips


def register_auth_routes(app, mysql):
    _db['mysql'] = mysql
    ready = {'table': False, 'admin': False, 'warned': None}
    ready_lock = threading.Lock()
    failures = defaultdict(deque)                    # ip -> times of recent failed logins
    failures_lock = threading.Lock()

    def cursor():
        return mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    def warn_once(message):
        if ready['warned'] != message:                   # not once per request: only when the problem changes
            ready['warned'] = message
            print('WARNING: ' + message)

    def ensure_admin(cur):
        """If no admin exists, create one from ADMIN_NAME / ADMIN_EMAIL / ADMIN_PASSWORD. While none can be created
        this is tried again on the next auth request (and .env is re-read), so fixing .env needs no restart."""
        cur.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1")
        if cur.fetchone():
            ready['admin'] = True
            return
        name, email, password = env_value('ADMIN_NAME'), env_value('ADMIN_EMAIL').lower(), env_value('ADMIN_PASSWORD')
        problems = []
        if not name:
            problems.append('ADMIN_NAME is empty or missing')
        if not EMAIL_RE.match(email):
            problems.append('ADMIN_EMAIL is empty, missing or not a valid email address')
        if len(password) < MIN_PASSWORD:
            problems.append(f'ADMIN_PASSWORD is missing or shorter than {MIN_PASSWORD} characters')
        if problems:
            warn_once('there is no admin account yet - ' + '; '.join(problems) + ' (check the .env file next to app.py).')
            return
        cur.execute("SELECT 1 FROM users WHERE email = %s", (email,))
        if cur.fetchone():
            warn_once('ADMIN_EMAIL is already registered as an ordinary account. Use another email.')
            return
        cur.execute("""INSERT INTO users (full_name, email, password_hash, role, status, approved_at)
                       VALUES (%s,%s,%s,'admin','approved',NOW())""",
                    (name[:100], email, generate_password_hash(password)))
        ready['admin'] = True
        print(f'Admin account created for {email}.')

    def ensure_ready():
        """Create the users table, and the first admin if there is none. Cheap once both exist; safe to call every time."""
        if ready['table'] and ready['admin']:
            return
        with ready_lock:
            cur = cursor()
            if not ready['table']:
                cur.execute(CREATE_USERS_SQL)
                ready['table'] = True
            if not ready['admin']:
                ensure_admin(cur)
            mysql.connection.commit()
            cur.close()

    def db_down():
        return _error('The database is offline. Start MySQL (XAMPP) and try again.', 503)

    def too_many(ip):
        """Seconds to wait if this IP has failed 5 logins in the last minute, else 0."""
        now = time.time()
        with failures_lock:
            q = failures[ip]
            while q and now - q[0] > FAIL_WINDOW:
                q.popleft()
            return int(FAIL_WINDOW - (now - q[0])) + 1 if len(q) >= MAX_FAILED else 0

    def note_failure(ip):
        with failures_lock:
            failures[ip].append(time.time())

    def clean_email(value):
        return value.strip().lower() if isinstance(value, str) else ''

    # ---------- register: creates a PENDING viewer, never logs in ----------
    @app.route('/api/auth/register', methods=['POST'])
    def auth_register():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return _error('Request body must be a JSON object')
        name = data.get('full_name').strip() if isinstance(data.get('full_name'), str) else ''
        email = clean_email(data.get('email'))
        password = data.get('password')
        errors = []
        if not name or len(name) > 100:
            errors.append('Full name is required (up to 100 characters).')
        if not EMAIL_RE.match(email) or len(email) > 120:
            errors.append('Enter a valid email address.')
        if not isinstance(password, str) or len(password) < MIN_PASSWORD:
            errors.append(f'Password must be at least {MIN_PASSWORD} characters.')
        elif len(password) > MAX_PASSWORD:
            errors.append(f'Password must be at most {MAX_PASSWORD} characters.')
        if errors:
            return jsonify({'error': ' '.join(errors), 'details': errors}), 400
        try:
            ensure_ready()
            cur = cursor()
            try:
                cur.execute("""INSERT INTO users (full_name, email, password_hash, role, status)
                               VALUES (%s,%s,%s,'viewer','pending')""", (name, email, generate_password_hash(password)))
                mysql.connection.commit()
            except MySQLdb.IntegrityError:
                mysql.connection.rollback()
                cur.close()
                return _error('That email is already registered.', 409)
            cur.close()
        except MySQLdb.Error:
            app.logger.exception('register failed')
            return db_down()
        return jsonify({'message': 'Registration received. You can log in once the admin approves your account.'}), 201

    # ---------- login ----------
    @app.route('/api/auth/login', methods=['POST'])
    def auth_login():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return _error('Request body must be a JSON object')
        ip = request.remote_addr or '?'
        wait = too_many(ip)
        if wait:
            return _error(f'Too many failed logins. Try again in {wait} seconds.', 429)
        email, password = clean_email(data.get('email')), data.get('password')
        if not isinstance(password, str) or len(password) > MAX_PASSWORD:
            password = ''
        try:
            ensure_ready()
            cur = cursor()
            cur.execute("SELECT id, full_name, email, password_hash, role, status FROM users WHERE email = %s", (email,))
            user = cur.fetchone()
            cur.close()
        except MySQLdb.Error:
            app.logger.exception('login failed')
            return db_down()
        valid = check_password_hash(user['password_hash'] if user else DUMMY_HASH, password) and user is not None
        if not valid:
            note_failure(ip)
            return _error('Invalid email or password', 401)
        if user['status'] == 'pending':
            note_failure(ip)
            return _error('Your account is awaiting admin approval', 403)
        if user['status'] == 'rejected':
            note_failure(ip)
            return _error('Your registration was not approved', 403)
        session.clear()                                  # a fresh session on every login
        session['uid'] = user['id']
        session.permanent = True
        return jsonify({'name': user['full_name'], 'email': user['email'], 'role': user['role']})

    # ---------- logout ----------
    @app.route('/api/auth/logout', methods=['POST'])
    def auth_logout():
        session.clear()
        return jsonify({'message': 'Logged out'})

    # ---------- who am I ----------
    @app.route('/api/auth/me')
    def auth_me():
        uid = session.get('uid')
        if not uid:
            return _error('Not logged in', 401)
        try:
            ensure_ready()
            cur = cursor()
            cur.execute("SELECT full_name, email, role, status FROM users WHERE id = %s", (uid,))
            user = cur.fetchone()
            cur.close()
        except MySQLdb.Error:
            app.logger.exception('me failed')
            return db_down()
        if user is None or user['status'] != 'approved':
            session.clear()
            return _error('Not logged in', 401)
        return jsonify({'name': user['full_name'], 'email': user['email'], 'role': user['role']})

    # ---------- admin: sign-up approvals ----------
    def public_user(row):
        """Never includes the password hash."""
        return {'id': row['id'], 'full_name': row['full_name'], 'email': row['email'], 'role': row['role'],
                'status': row['status'], 'created_at': str(row['created_at'])[:19] if row['created_at'] else None,
                'approved_at': str(row['approved_at'])[:19] if row['approved_at'] else None}

    @app.route('/api/admin/users')
    @admin_required
    def admin_list_users():
        cur = cursor()
        cur.execute("SELECT id, full_name, email, role, status, created_at, approved_at FROM users ORDER BY created_at, id")
        rows = [public_user(r) for r in cur.fetchall()]
        cur.close()
        return jsonify({status: [u for u in rows if u['status'] == status] for status in ('pending', 'approved', 'rejected')})

    def set_user_status(user_id, new_status):
        me = current_user()
        cur = cursor()
        cur.execute("SELECT id, full_name, email, role, status, created_at, approved_at FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        if row is None:
            cur.close()
            return _error('User not found', 404)
        if row['id'] == me['id']:
            cur.close()
            return _error('You cannot change your own account', 403)
        if row['role'] == 'admin':
            cur.close()
            return _error('Admin accounts cannot be changed here', 403)
        previous = row['status']
        # no rowcount check: MySQL reports 0 affected rows when nothing changes (e.g. approving twice)
        if new_status == 'approved':
            cur.execute("UPDATE users SET status = 'approved', approved_at = NOW() WHERE id = %s AND role <> 'admin'", (user_id,))
        else:
            cur.execute("UPDATE users SET status = 'rejected' WHERE id = %s AND role <> 'admin'", (user_id,))
        mysql.connection.commit()
        cur.execute("SELECT id, full_name, email, role, status, created_at, approved_at FROM users WHERE id = %s", (user_id,))
        updated = public_user(cur.fetchone())
        cur.execute("SELECT COUNT(*) AS n FROM users WHERE status = 'pending'")
        pending = cur.fetchone()['n']
        cur.close()
        return jsonify({'user': updated, 'previous_status': previous, 'pending_count': pending})

    @app.route('/api/admin/users/<int:user_id>/approve', methods=['POST'])
    @admin_required
    def admin_approve_user(user_id):
        return set_user_status(user_id, 'approved')

    @app.route('/api/admin/users/<int:user_id>/reject', methods=['POST'])
    @admin_required
    def admin_reject_user(user_id):
        return set_user_status(user_id, 'rejected')

    def init_accounts():
        """Called once at startup. If MySQL is down now, the first auth request tries again."""
        try:
            with app.app_context():
                ensure_ready()
        except Exception as e:
            print(f'WARNING: could not prepare the users table yet ({type(e).__name__}); will retry on the first login.')

    return init_accounts
