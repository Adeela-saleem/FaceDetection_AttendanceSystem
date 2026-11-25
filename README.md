# Face Recognition Attendance System (full-stack web app)

FastAPI backend + plain HTML/JS frontend + SQLite. Staff log in with roles, students are registered once with a few photos, and attendance is marked automatically from the **browser camera** (live) or an uploaded class photo.

## Features

- **Landing page**: visitors who are not logged in see a public landing page at `/` (introduction, features, Login button and a simulated **Try Demo** that uses made-up students, no camera and no database). Logged-in users go straight to the dashboard as before.
- **Login + roles**: Admin and Teacher (permissions below). First run asks you to create the single Admin account.
- **Pages**: Dashboard · Take Attendance (live camera / photo) · Students · Register Student · Records (filters, CSV, manual entry) · Users · Settings (+ activity log)
- **Recognition**: dlib face encodings (pretrained, no training). Live mode needs the same face in N scans in a row before saving, so one bad frame can't mark the wrong person.
- **Late marking**: arrivals after a cutoff time (Settings) are stored as *Late*.
- **Security**: scrypt password hashing, HttpOnly session cookies (stored hashed in DB), login rate limit, server-side permission checks on every endpoint, CSV formula-injection protection, activity log.

| | Admin | Teacher |
|---|:-:|:-:|
| View dashboard / students / records / CSV | ✅ all sections | ✅ assigned sections |
| Take attendance (camera / photo / manual) | ✅ all sections | ✅ assigned sections |
| Create / edit sections, assign teachers | ✅ | – |
| Register / edit / delete students | ✅ | – |
| Manage users | Teachers | – |
| System settings + activity log | ✅ | – |

## Sections, roll nos and the Late rule

- **Sections**: every student belongs to a section (e.g. `BSCS-6A`). Create them on the *Sections* page. Students page is grouped by section; tick students and use *Move to section* for many at once.
- **Roll no**: filled automatically (01, 02, 03 ...) per section when a student gets a section; you can still type one yourself (must be unique inside the section).
- **Teachers** are assigned to sections on the *Users* or *Sections* page. A teacher only sees students and attendance from assigned sections and can delete only records they created.
- **Late rule = class time**: each section has a class time and a "late after N minutes" (default 10). Example class 10:00-11:00, late after 10:
  - 09:30 to 10:10 -> Present
  - 10:10 to 11:00 -> Late (so 10:50 is Late)
  - before 09:30 or after 11:00 -> attendance is not saved (outside class hours)
  The "opens N minutes early" value is in Settings (default 30). If a section has no class time, the old global *Late after* setting is used and there is no time limit.

## Setup

```bash
pip install -r requirements.txt
uvicorn api:app --reload          # open http://localhost:8000
```

Open it via **http://localhost:8000** (not your LAN IP) so the camera works. First visit → create the Admin → add Teachers from *Users*, students from *Register Student*.

Forgot the Admin password?  `python manage.py reset-password <username>`

Already have photos in `Known_Faces/`?  `python import_known_faces.py` (an existing `attendance.db` is upgraded automatically, nothing is lost).

## Camera stuck on "loading"? (the old Streamlit problem)

The browser only gives camera access on **HTTPS or localhost**, and only to **one program at a time**. The new pages never spin forever; they show the exact reason. Usual causes:

1. Opened via `http://192.168.x.x:...` or any plain-http address → use `http://localhost:8000`, or put the app behind HTTPS.
2. Camera permission blocked → click the camera icon in the address bar → Allow → reload.
3. Another program holds the camera: `recognize_live.py`, Zoom/Meet, another tab → close it.

`recognize_live.py` (OpenCV window) still works for a dedicated attendance PC, but don't run it together with the browser camera.

## Tuning (Settings page, Admin)

| Problem | Change |
|---|---|
| Real person shows "Unknown" | raise threshold slightly (0.5 → 0.55), add more photos |
| Wrong person matched | lower threshold (0.5 → 0.45) |
| Late students not marked late | set *Late after* time |

Speed/accuracy knobs for the live script (`FRAME_SCALE`, `DETECTION_MODEL`) stay in `config.py`.

## Project structure

```
api.py                 FastAPI app: auth, users, students, attendance, settings, audit + serves pages
database.py            SQLite: students, encodings, attendance, users, sessions, settings, audit
security.py            password hashing, roles + permission table
face_engine.py         detection, encoding, matching (dlib via face_recognition)
manage.py              CLI: create Admin / reset a password
recognize_live.py      optional OpenCV live window (dedicated PC)
import_known_faces.py  import an existing Known_Faces/ folder
static/                frontend: login, dashboard, attendance, students, register, records, users, settings
tests/                 pytest suite (runs without dlib using a fake detector)
```

## Tests

```bash
pip install pytest httpx
pytest
```

## Deployment

- **Local / institute PC (recommended first):** `uvicorn api:app --host 0.0.0.0 --port 8000` and open from that PC via localhost. For other devices you need HTTPS (reverse proxy such as Caddy/nginx, or a tunnel), then set `ATTENDANCE_COOKIE_SECURE=1`.
- **Docker:** `docker build -t face-attendance .` → `docker run -p 8000:8000 -v attendance_data:/data face-attendance`. The first build compiles dlib (several minutes). Put it behind HTTPS.
- Env vars: `ATTENDANCE_DB`, `ATTENDANCE_AVATARS`, `ATTENDANCE_SESSION_HOURS`, `ATTENDANCE_COOKIE_SECURE`, `ATTENDANCE_DEBUG` (enables `/docs`).

## Limitations / next steps

- No liveness detection: a printed photo or phone screen can fool it. Blink/liveness check is the main security upgrade.
- Recognition depends on lighting and camera quality; test in the real room first.
- Face data is sensitive personal data: get students' consent, protect the database and back it up. Only a small thumbnail per student is stored besides the encodings.
- Ideas: per-class timetable/subjects, parent email/WhatsApp alerts, forced password change for new users, PostgreSQL for multi-server setups.

## Windows quick start (webcam + face engine)

1. Install **64-bit Python 3.10 or 3.11**.
2. Double-click `START_WINDOWS.bat`. It creates `.venv`, installs dependencies, checks the real face engine, and starts the website.
3. Open `http://localhost:8000` in Chrome/Edge and allow camera access when prompted.
4. Create the first Admin (only on a fresh database), register students with clear face photos, then open Take Attendance and press **Start camera**.
5. Visit `http://localhost:8000/api/health` to check frontend/database/face-engine readiness. The face engine must report `ready` before recognition can work.

### Windows face-engine setup

`face_recognition` depends on `dlib`, which may need a C++ compiler when pip cannot find a compatible wheel. If installation fails, install Visual Studio Build Tools with **Desktop development with C++** (including the Windows SDK), reopen a terminal, activate `.venv`, and run:

```powershell
python -m pip install --upgrade pip
python -m pip install "setuptools<81"
python -m pip install dlib
python -m pip install face_recognition
python check_environment.py
```

Do not consider the app ready for attendance until `python check_environment.py` succeeds and `/api/health` reports `face_engine: ready`. The browser camera is accessed by the browser on the computer where the website is opened; this workspace cannot grant permission to your physical webcam remotely. Use `localhost` or HTTPS (not a plain HTTP LAN IP). Close Zoom/Meet/other camera apps while testing.

The optional `recognize_live.py` OpenCV camera script and the browser camera must not use the webcam simultaneously.
