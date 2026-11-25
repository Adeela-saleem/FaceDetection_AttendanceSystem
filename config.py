"""Central settings. Change values here (or via environment variables).

Runtime settings that admins can change from the web UI (threshold, late time, institute name...)
live in the database -- the values below are only the defaults.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("ATTENDANCE_DB", BASE_DIR / "attendance.db"))
PHOTOS_DIR = Path(os.environ.get("ATTENDANCE_PHOTOS", BASE_DIR / "Known_Faces"))   # only for import_known_faces.py
AVATAR_DIR = Path(os.environ.get("ATTENDANCE_AVATARS", BASE_DIR / "avatars"))      # small face thumbnails

# --- Web server / security ---
SESSION_HOURS = int(os.environ.get("ATTENDANCE_SESSION_HOURS", 12))
COOKIE_SECURE = os.environ.get("ATTENDANCE_COOKIE_SECURE", "0") == "1"   # set to 1 when served over HTTPS
DEBUG = os.environ.get("ATTENDANCE_DEBUG", "0") == "1"                   # enables /docs

# --- Camera (live OpenCV script only; the web app uses the browser camera) ---
CAMERA_INDEX = int(os.environ.get("ATTENDANCE_CAMERA", 0))  # 0 = laptop webcam, or an RTSP URL string
FRAME_SCALE = 0.5            # shrink frame before detection (speed vs accuracy)
PROCESS_EVERY_N_FRAMES = 2   # run recognition on every Nth frame

# --- Recognition ---
DETECTION_MODEL = "hog"      # "hog" (CPU, fast) or "cnn" (GPU, more accurate)
MAX_IMAGE_SIDE = 1000        # uploaded photos are shrunk to this many pixels (longest side)

# --- Defaults for settings editable in the UI (stored in DB once changed) ---
DEFAULT_SETTINGS = {
    "institute_name": "Face Attendance",
    "threshold": float(os.environ.get("ATTENDANCE_THRESHOLD", 0.5)),  # distance below this = same person
    "late_after": "09:00",       # "HH:MM"; only for students whose section has NO class time. Empty = never late
    "confirm_frames": 3,         # live camera: same person must be seen this many scans in a row
    "early_minutes": 30,         # attendance opens this many minutes before a section's class starts
}
