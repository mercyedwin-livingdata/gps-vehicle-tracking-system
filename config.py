"""Single source of truth for company, geography and fleet data.

Used by app.py (/seed, /api/config), trips.py and simulator.py.
Coordinates are approximate town centres (decimal degrees, WGS84).
"""

COMPANY_NAME = "GUO Transport"

# Map opens centred between Awka and Onitsha
MAP_CENTER = {'lat': 6.1880, 'lng': 6.9270, 'zoom': 10}

# ── ANAMBRA STATE TOWNS (approximate) ───────────
TOWNS = {
    'Onitsha':   {'lat': 6.1667, 'lng': 6.7833},
    'Awka':      {'lat': 6.2100, 'lng': 7.0700},
    'Nnewi':     {'lat': 6.0167, 'lng': 6.9167},
    'Nkpor':     {'lat': 6.1433, 'lng': 6.8350},
    'Oba':       {'lat': 6.0900, 'lng': 6.8600},
    'Ekwulobia': {'lat': 6.0000, 'lng': 7.0833},
    'Ihiala':    {'lat': 5.8556, 'lng': 6.8628},
    'Okija':     {'lat': 5.9400, 'lng': 6.8300},
    'Ozubulu':   {'lat': 5.9900, 'lng': 6.8900},
}

# ── THE COMPANY FLEET ────────────────────────────
# plate = unique identifier. name = vehicle model (may repeat across plates).
# seats = passenger seats (Front + rows of three), driver not counted. driver = the vehicle's usual driver.
# start = the town the vehicle is first placed in when /seed inserts it. Plates and names are made up.
# /seed only INSERTS vehicles whose plate is missing; it never updates or deletes anything.
FLEET = [
    {'id': 1, 'plate': 'AWK-234-XA', 'name': 'Toyota Sienna',            'seats': 7,  'driver': 'Chinedu Okeke',   'start': 'Onitsha'},
    {'id': 2, 'plate': 'NNW-891-KB', 'name': 'Toyota Hiace Bus',         'seats': 13, 'driver': 'Ifeanyi Nwosu',   'start': 'Nnewi'},
    {'id': 3, 'plate': 'EKW-102-PQ', 'name': 'Toyota Camry',             'seats': 4,  'driver': 'Obinna Eze',      'start': 'Ekwulobia'},
    {'id': 4, 'plate': 'ONS-557-MN', 'name': 'Toyota Corolla',           'seats': 4,  'driver': 'Uche Anyanwu',    'start': 'Ihiala'},
    {'id': 5, 'plate': 'OBA-346-RT', 'name': 'Toyota Highlander',        'seats': 7,  'driver': 'Emeka Nnaji',     'start': 'Oba'},
    {'id': 6, 'plate': 'AWK-778-LD', 'name': 'Mercedes-Benz Sprinter Bus', 'seats': 18, 'driver': 'Chukwuma Dike',   'start': 'Awka'},
]
