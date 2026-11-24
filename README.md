# Real-Time Vehicle Tracking and Monitoring System

A web-based fleet monitoring dashboard that tracks vehicles live on a map, manages trips and drivers, and generates end-of-day reports. Built as my final-year Computer Science project at Legacy University, Okija.

## Features
- Live vehicle tracking on an interactive map (Leaflet.js + OpenStreetMap), refreshing every 10 seconds
- Trip and driver management with a per-day "Add Vehicle" trip form
- End-of-day reports with KPI cards, downloadable as PDF and shareable by email
- Role-based access: one admin and approved read-only viewers
- Machine learning module (Random Forest, Scikit-learn) trained on simulated data
- Light and dark mode

## Tech Stack
Python (Flask), MySQL (XAMPP), JavaScript, HTML, CSS, Leaflet.js, Scikit-learn

## Screenshots
![Dashboard](screenshots/dashboard.png)
![Trip form](screenshots/trip-form.png)
![Report](screenshots/report.png)

## How to Run
1. Clone the repository and open the folder.
2. Create and activate a virtual environment:
   python -m venv .venv
   .venv\Scripts\activate
3. Install dependencies: pip install -r requirements.txt
4. Start MySQL in XAMPP, create a database named gps_tracking, and import schema.sql.
5. Copy .env.example to .env and fill in your own values (SMTP details, secret key, admin email and password).
6. Run the simulator: python simulator.py
7. In a second terminal, run the app: python app.py
8. Open http://localhost:5000

## Notes
Vehicle positions and fuel data are simulated for demonstration.

## What I Learned
Designing a full-stack system end to end: database design, a Flask backend, live map updates, role-based access, and applying a machine learning model to fleet data.

## Author
Mercy Edwin
