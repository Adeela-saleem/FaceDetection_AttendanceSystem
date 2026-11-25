"""SQLite storage: students, face encodings, attendance, users, sessions, settings, audit log."""
import hashlib
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

import numpy as np

import config


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH)
    # plain dicts instead of tuples
    conn.row_factory = lambda cur, row: {d[0]: v for d, v in zip(cur.description, row)}
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _add_column(c, table, column, ddl):
    """Tiny migration helper so an existing attendance.db keeps working after upgrades."""
    cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _migrate_user_roles(c):
    schema = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()["sql"]
    if "'super_admin'" not in schema and "'principal'" not in schema:
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_single_admin ON users(role) WHERE role = 'admin'")
        return

    admin_candidates = c.execute(
        "SELECT COUNT(*) AS n FROM users WHERE role IN ('super_admin', 'admin')"
    ).fetchone()["n"]
    if admin_candidates > 1:
        raise RuntimeError(
            "Cannot migrate roles: multiple Admin/Super Admin accounts exist. "
            "Resolve the accounts before restarting."
        )

    seq = c.execute("SELECT seq FROM sqlite_sequence WHERE name = 'users'").fetchone()
    previous_sequence = seq["seq"] if seq else 0
    c.execute("PRAGMA foreign_keys = OFF")
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("""
            CREATE TABLE users_new (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
                full_name     TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL CHECK (role IN ('admin', 'teacher')),
                active        INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL,
                last_login    TEXT
            )
        """)
        c.execute("""
            INSERT INTO users_new (id, username, full_name, password_hash, role, active, created_at, last_login)
            SELECT id, username, full_name, password_hash,
                   CASE role
                       WHEN 'super_admin' THEN 'admin'
                       WHEN 'principal' THEN 'teacher'
                       ELSE role
                   END,
                   active, created_at, last_login
            FROM users
        """)
        c.execute("DROP TABLE users")
        c.execute("ALTER TABLE users_new RENAME TO users")
        c.execute("CREATE UNIQUE INDEX users_single_admin ON users(role) WHERE role = 'admin'")
        sequence_update = c.execute(
            "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'users'", (previous_sequence,))
        if sequence_update.rowcount == 0 and previous_sequence:
            c.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('users', ?)", (previous_sequence,))
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.execute("PRAGMA foreign_keys = ON")


def init_db():
    with get_conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS students (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL UNIQUE,
                roll_no    TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS encodings (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                encoding   BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attendance (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                date       TEXT NOT NULL,
                time       TEXT NOT NULL,
                confidence REAL,
                UNIQUE (student_id, date)
            );
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
                full_name     TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL CHECK (role IN ('admin','teacher')),
                active        INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL,
                last_login    TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires    INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sections (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                name           TEXT NOT NULL UNIQUE COLLATE NOCASE,
                class_start    TEXT,
                class_end      TEXT,
                late_after_min INTEGER NOT NULL DEFAULT 10,
                created_at     TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS teacher_sections (
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
                PRIMARY KEY (user_id, section_id)
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       TEXT NOT NULL,
                username TEXT,
                action   TEXT NOT NULL,
                detail   TEXT
            );
            """
        )
        _migrate_user_roles(c)
        _add_column(c, "students", "class_name", "TEXT")
        _add_column(c, "attendance", "status", "TEXT NOT NULL DEFAULT 'present'")
        _add_column(c, "students", "section_id", "INTEGER REFERENCES sections(id) ON DELETE SET NULL")
        _add_column(c, "attendance", "section_id", "INTEGER")
        _add_column(c, "attendance", "teacher_id", "INTEGER")
        _migrate_attendance_sessions(c)
        c.execute("""
            UPDATE attendance
            SET section_id = (
                SELECT section_id FROM students WHERE students.id = attendance.student_id
            )
            WHERE section_id IS NULL
        """)
        c.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS attendance_teacher_section_session
            ON attendance(student_id, date, time, section_id, teacher_id)
            WHERE section_id IS NOT NULL AND teacher_id IS NOT NULL
        """)
        _migrate_classes_to_sections(c)


def _migrate_attendance_sessions(c):
    """Remove the old one-record-per-student-per-day constraint and add session ownership columns.

    Runs ONLY for a legacy table that still has the old UNIQUE(student_id, date) *constraint* (index origin 'u').
    The newer partial unique index (origin 'c') must not trigger it, otherwise the whole attendance table
    would be rebuilt on every start-up. The rebuild is one atomic transaction: either every record is copied
    over exactly as it was (teacher_id stays NULL where it was NULL), or nothing changes.
    """
    legacy_constraint = any(r["unique"] and r["origin"] == "u" for r in c.execute("PRAGMA index_list('attendance')"))
    if not legacy_constraint:
        return
    legacy_columns = {r["name"] for r in c.execute("PRAGMA table_info(attendance)").fetchall()}
    section_col = ", section_id" if "section_id" in legacy_columns else ""
    teacher_col = ", teacher_id" if "teacher_id" in legacy_columns else ""
    status_expr = "COALESCE(status, 'present')" if "status" in legacy_columns else "'present'"
    c.commit()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("ALTER TABLE attendance RENAME TO attendance_legacy")
        c.execute("""
            CREATE TABLE attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                confidence REAL,
                status TEXT NOT NULL DEFAULT 'present',
                section_id INTEGER,
                teacher_id INTEGER
            )
        """)
        c.execute(
            f"INSERT INTO attendance (id, student_id, date, time, confidence, status{section_col}{teacher_col}) "
            f"SELECT id, student_id, date, time, confidence, {status_expr}{section_col}{teacher_col} "
            f"FROM attendance_legacy"
        )
        copied = c.execute("SELECT COUNT(*) AS n FROM attendance").fetchone()["n"]
        original = c.execute("SELECT COUNT(*) AS n FROM attendance_legacy").fetchone()["n"]
        if copied != original:
            raise RuntimeError(f"Attendance migration copied {copied} of {original} rows; aborted, nothing changed.")
        c.execute("DROP TABLE attendance_legacy")
        c.commit()
    except Exception:
        c.rollback()
        raise


def _migrate_classes_to_sections(c):
    """Old versions stored a free-text class name. Turn each distinct name into a real section (once),
    then give every sectioned student without a roll no the next free one."""
    now = datetime.now().isoformat(timespec="seconds")
    for r in c.execute("SELECT DISTINCT class_name FROM students "
                       "WHERE section_id IS NULL AND class_name IS NOT NULL AND TRIM(class_name) != ''").fetchall():
        name = r["class_name"].strip()
        row = c.execute("SELECT id FROM sections WHERE name = ?", (name,)).fetchone()
        sid = row["id"] if row else c.execute(
            "INSERT INTO sections (name, created_at) VALUES (?, ?)", (name, now)).lastrowid
        c.execute("UPDATE students SET section_id = ? WHERE section_id IS NULL AND TRIM(class_name) = ?", (sid, name))
    for sec in c.execute("SELECT DISTINCT section_id FROM students WHERE section_id IS NOT NULL "
                         "AND (roll_no IS NULL OR TRIM(roll_no) = '')").fetchall():
        for st in c.execute("SELECT id FROM students WHERE section_id = ? AND (roll_no IS NULL OR TRIM(roll_no) = '') "
                            "ORDER BY name COLLATE NOCASE", (sec["section_id"],)).fetchall():
            c.execute("UPDATE students SET roll_no = ? WHERE id = ?", (_next_roll_no(c, sec["section_id"]), st["id"]))


# ---------------- settings ----------------
def get_settings():
    d = config.DEFAULT_SETTINGS
    with get_conn() as c:
        stored = {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM settings")}
    return {
        "institute_name": stored.get("institute_name", d["institute_name"]),
        "threshold": float(stored.get("threshold", d["threshold"])),
        "late_after": stored.get("late_after", d["late_after"]),
        "confirm_frames": int(stored.get("confirm_frames", d["confirm_frames"])),
        "early_minutes": int(stored.get("early_minutes", d["early_minutes"])),
    }


def set_settings(values):
    with get_conn() as c:
        for k, v in values.items():
            c.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, str(v)))


def attendance_status(now=None):
    """Fallback for students with no class time: 'late' if after the global cutoff, else 'present'."""
    cutoff = get_settings()["late_after"]
    now = now or datetime.now()
    return "late" if cutoff and now.strftime("%H:%M") > cutoff else "present"


def _at(now, hhmm):
    h, m = map(int, hhmm.split(":"))
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


def status_for_student(student, now=None):
    """Decides what happens if `student` is scanned right now. Returns (status, info):

    status = 'present' | 'closed'
    - Section has a class time (e.g. 10:00-11:00):
        from (start - early_minutes) to end -> present
        before that window / after class end -> closed (nothing is saved)
    - No class time (or no section): always 'present'.
    """
    now = now or datetime.now()
    start, end = student.get("class_start"), student.get("class_end")
    if not (start and end):
        return attendance_status(now), None
    early = get_settings()["early_minutes"]
    info = {"start": start, "end": end}
    if now < _at(now, start) - timedelta(minutes=early) or now > _at(now, end):
        return "closed", info
    cutoff = _at(now, start) + timedelta(minutes=10)
    return ("late" if now > cutoff else "present"), info


# ---------------- sections ----------------
def _in(col, ids):
    ids = list(ids)
    return f"{col} IN ({','.join('?' * len(ids))})", ids


def _next_roll_no(c, section_id):
    """Next free roll no in a section: 01, 02, ... (non-numeric roll nos like 'CS-1' are ignored)."""
    nums = [int(r["roll_no"]) for r in c.execute("SELECT roll_no FROM students WHERE section_id = ?", (section_id,))
            if r["roll_no"] and str(r["roll_no"]).strip().isdigit()]
    return f"{max(nums, default=0) + 1:02d}"


def _roll_taken(c, section_id, roll_no, exclude_id=None):
    if not section_id or not roll_no:
        return False
    return c.execute("SELECT 1 FROM students WHERE section_id = ? AND roll_no = ? COLLATE NOCASE AND id != ?",
                     (section_id, roll_no, exclude_id or 0)).fetchone() is not None


def create_section(name, class_start=None, class_end=None, late_after_min=10, teacher_ids=()):
    name = name.strip()
    if not name:
        raise ValueError("Enter a section name")
    with get_conn() as c:
        if c.execute("SELECT 1 FROM sections WHERE name = ?", (name,)).fetchone():
            raise ValueError("A section with this name already exists")
        sid = c.execute("INSERT INTO sections (name, class_start, class_end, late_after_min, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (name, class_start or None, class_end or None, late_after_min,
                         datetime.now().isoformat(timespec="seconds"))).lastrowid
        _set_teachers(c, sid, teacher_ids)
        return sid


def update_section(section_id, name, class_start, class_end, late_after_min, teacher_ids=None):
    name = name.strip()
    with get_conn() as c:
        clash = c.execute("SELECT id FROM sections WHERE name = ?", (name,)).fetchone()
        if clash and clash["id"] != section_id:
            raise ValueError("Another section with this name already exists")
        c.execute("UPDATE sections SET name = ?, class_start = ?, class_end = ?, late_after_min = ? WHERE id = ?",
                  (name, class_start or None, class_end or None, late_after_min, section_id))
        if teacher_ids is not None:
            _set_teachers(c, section_id, teacher_ids)


def _set_teachers(c, section_id, teacher_ids):
    c.execute("DELETE FROM teacher_sections WHERE section_id = ?", (section_id,))
    for uid in set(teacher_ids):
        c.execute("INSERT OR IGNORE INTO teacher_sections (user_id, section_id) VALUES (?, ?)", (uid, section_id))


def set_teacher_sections(user_id, section_ids):
    with get_conn() as c:
        c.execute("DELETE FROM teacher_sections WHERE user_id = ?", (user_id,))
        for section_id in set(section_ids):
            c.execute("INSERT OR IGNORE INTO teacher_sections (user_id, section_id) VALUES (?, ?)",
                      (user_id, section_id))


def delete_section(section_id):
    """Students of this section stay, they just become 'unassigned'."""
    with get_conn() as c:
        c.execute("DELETE FROM sections WHERE id = ?", (section_id,))


def get_section(section_id):
    with get_conn() as c:
        return c.execute("SELECT * FROM sections WHERE id = ?", (section_id,)).fetchone()


def get_section_by_name(name):
    with get_conn() as c:
        return c.execute("SELECT * FROM sections WHERE name = ? COLLATE NOCASE", (name.strip(),)).fetchone()


def list_sections(section_ids=None):
    """All sections (or only `section_ids`) with student count and assigned teachers."""
    if section_ids is not None and not list(section_ids):
        return []
    sql = ("SELECT sec.*, COUNT(s.id) AS n_students FROM sections sec "
           "LEFT JOIN students s ON s.section_id = sec.id")
    params = []
    if section_ids is not None:
        cond, params = _in("sec.id", section_ids)
        sql += f" WHERE {cond}"
    sql += " GROUP BY sec.id ORDER BY sec.name COLLATE NOCASE"
    with get_conn() as c:
        rows = c.execute(sql, params).fetchall()
        teachers = {}
        for t in c.execute("SELECT ts.section_id, u.id, u.full_name, u.username FROM teacher_sections ts "
                           "JOIN users u ON u.id = ts.user_id ORDER BY u.full_name COLLATE NOCASE"):
            teachers.setdefault(t["section_id"], []).append(
                {"id": t["id"], "full_name": t["full_name"], "username": t["username"]})
    return [{**r, "teachers": teachers.get(r["id"], [])} for r in rows]


def teacher_section_ids(user_id):
    with get_conn() as c:
        return [r["section_id"] for r in c.execute(
            "SELECT section_id FROM teacher_sections WHERE user_id = ?", (user_id,))]


def teacher_sections_map():
    """{user_id: [section names]} for the Users page."""
    with get_conn() as c:
        out = {}
        for r in c.execute("SELECT ts.user_id, sec.name FROM teacher_sections ts "
                           "JOIN sections sec ON sec.id = ts.section_id ORDER BY sec.name COLLATE NOCASE"):
            out.setdefault(r["user_id"], []).append(r["name"])
        return out


# ---------------- students ----------------
# `class_name` in query results is the *section name* (kept so older pages/CSV keep working).
_STUDENT_COLS = ("s.id, s.name, s.roll_no, s.section_id, s.created_at, sec.name AS class_name, "
                 "sec.class_start, sec.class_end, sec.late_after_min")
_STUDENT_FROM = "FROM students s LEFT JOIN sections sec ON sec.id = s.section_id"


def add_student(name, roll_no=None, section_id=None):
    name = name.strip()
    if not name:
        raise ValueError("Name cannot be empty")
    roll_no = (roll_no or "").strip() or None
    with get_conn() as c:
        if section_id and not roll_no:
            roll_no = _next_roll_no(c, section_id)            # auto roll no
        elif _roll_taken(c, section_id, roll_no):
            raise ValueError(f"Roll number {roll_no} is already assigned in this section")
        cur = c.execute("INSERT INTO students (name, roll_no, section_id, created_at) VALUES (?, ?, ?, ?)",
                        (name, roll_no, section_id or None, datetime.now().isoformat(timespec="seconds")))
        return cur.lastrowid


def update_student(student_id, name, roll_no, section_id):
    roll_no = (roll_no or "").strip() or None
    section_id = section_id or None
    with get_conn() as c:
        old = c.execute("SELECT roll_no, section_id FROM students WHERE id = ?", (student_id,)).fetchone()
        if section_id and (not roll_no or (roll_no == old["roll_no"] and old["section_id"] != section_id
                                           and _roll_taken(c, section_id, roll_no, student_id))):
            roll_no = _next_roll_no(c, section_id)            # empty, or old roll no clashes in the new section
        elif _roll_taken(c, section_id, roll_no, student_id):
            raise ValueError(f"Roll number {roll_no} is already assigned in this section")
        c.execute("UPDATE students SET name = ?, roll_no = ?, section_id = ? WHERE id = ?",
                  (name.strip(), roll_no, section_id, student_id))


def assign_section(student_ids, section_id):
    """Move many students into a section (or out of all sections if None). Missing / clashing roll nos get
    the next free number, in alphabetical order. Returns how many students were moved."""
    section_id = section_id or None
    with get_conn() as c:
        rows = []
        for ids in (student_ids[i:i + 500] for i in range(0, len(student_ids), 500)):
            cond, params = _in("id", ids)
            rows += c.execute(f"SELECT id, roll_no FROM students WHERE {cond}", params).fetchall()
        rows.sort(key=lambda r: c.execute("SELECT name FROM students WHERE id = ?", (r["id"],)).fetchone()["name"].lower())
        for r in rows:
            roll = r["roll_no"]
            if section_id and (not roll or _roll_taken(c, section_id, roll, r["id"])):
                roll = _next_roll_no(c, section_id)
            c.execute("UPDATE students SET section_id = ?, roll_no = ? WHERE id = ?", (section_id, roll, r["id"]))
        return len(rows)


def get_student(student_id):
    with get_conn() as c:
        return c.execute(f"SELECT {_STUDENT_COLS} {_STUDENT_FROM} WHERE s.id = ?", (student_id,)).fetchone()


def get_student_by_name(name):
    with get_conn() as c:
        return c.execute(f"SELECT {_STUDENT_COLS} {_STUDENT_FROM} WHERE s.name = ? COLLATE NOCASE",
                         (name.strip(),)).fetchone()


def list_students(q=None, class_name=None, section_id=None, section_ids=None):
    """section_id: one section (-1 = students with no section). section_ids: restrict to these (teacher scope)."""
    if section_ids is not None and not list(section_ids):
        return []
    sql = f"""SELECT {_STUDENT_COLS}, COUNT(e.id) AS n_photos
              {_STUDENT_FROM} LEFT JOIN encodings e ON e.student_id = s.id WHERE 1=1"""
    params = []
    if q:
        sql += " AND (s.name LIKE ? OR s.roll_no LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    if class_name:
        sql += " AND sec.name = ?"
        params.append(class_name)
    if section_id == -1:
        sql += " AND s.section_id IS NULL"
    elif section_id:
        sql += " AND s.section_id = ?"
        params.append(section_id)
    if section_ids is not None:
        cond, ids = _in("s.section_id", section_ids)
        sql += f" AND {cond}"
        params += ids
    sql += " GROUP BY s.id ORDER BY sec.name COLLATE NOCASE, CAST(s.roll_no AS INTEGER), s.roll_no, s.name COLLATE NOCASE"
    with get_conn() as c:
        return c.execute(sql, params).fetchall()


def list_classes():
    with get_conn() as c:
        return [r["name"] for r in c.execute("SELECT name FROM sections ORDER BY name COLLATE NOCASE")]


def delete_student(student_id):
    with get_conn() as c:
        c.execute("DELETE FROM students WHERE id = ?", (student_id,))


# ---------------- encodings ----------------
def add_encoding(student_id, encoding):
    blob = np.asarray(encoding, dtype=np.float64).tobytes()
    with get_conn() as c:
        c.execute("INSERT INTO encodings (student_id, encoding) VALUES (?, ?)", (student_id, blob))


def delete_encodings(student_id):
    with get_conn() as c:
        c.execute("DELETE FROM encodings WHERE student_id = ?", (student_id,))


def load_all_encodings():
    """Returns (encodings[n,128], student_ids[n], {student_id: name})."""
    with get_conn() as c:
        rows = c.execute("SELECT student_id, encoding FROM encodings").fetchall()
        names = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM students")}
    if not rows:
        return np.empty((0, 128)), [], names
    encs = np.vstack([np.frombuffer(r["encoding"], dtype=np.float64) for r in rows])
    ids = [r["student_id"] for r in rows]
    return encs, ids, names


# ---------------- attendance ----------------
def mark_attendance_ex(student_id, confidence=None, status=None, day=None, at=None, section_id=None, teacher_id=None):
    """Mark present for a day (default today). Returns (newly_marked, status_stored)."""
    now = datetime.now()
    status = status or "present"
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO attendance (student_id, date, time, confidence, status, section_id, teacher_id) "
            "SELECT ?, ?, ?, ?, ?, ?, ? "
            "WHERE NOT EXISTS (SELECT 1 FROM attendance "
            "WHERE student_id=? AND date=? AND time=? AND section_id IS ? AND teacher_id IS ?)",
            (student_id, day or now.strftime("%Y-%m-%d"), at or now.strftime("%H:%M:%S"), confidence, status,
             section_id, teacher_id, student_id, day or now.strftime("%Y-%m-%d"),
             at or now.strftime("%H:%M:%S"), section_id, teacher_id),
        )
        return cur.rowcount == 1, status


def mark_attendance(student_id, confidence=None, status=None, day=None):
    """Returns True if newly marked, False if already marked that day."""
    return mark_attendance_ex(student_id, confidence, status, day)[0]


def get_attendance_record(record_id):
    with get_conn() as c:
        return c.execute("SELECT id, student_id, date, time, status, section_id, teacher_id FROM attendance WHERE id = ?",
                         (record_id,)).fetchone()


def get_attendance(start=None, end=None, student_id=None, class_name=None, status=None, section_ids=None):
    if section_ids is not None and not list(section_ids):
        return []
    q = """SELECT a.id, a.student_id, s.name, s.roll_no, sec.name AS class_name, a.date, a.time, a.status, a.confidence,
                  a.section_id, a.teacher_id
           FROM attendance a JOIN students s ON s.id = a.student_id
           LEFT JOIN sections sec ON sec.id = a.section_id WHERE 1=1"""
    params = []
    for cond, val in (("a.date >= ?", start and str(start)), ("a.date <= ?", end and str(end)),
                      ("a.student_id = ?", student_id), ("sec.name = ?", class_name),
                      ("a.status = ?", status)):
        if val:
            q += f" AND {cond}"
            params.append(val)
    if section_ids is not None:
        cond, ids = _in("a.section_id", section_ids)
        q += f" AND {cond}"
        params += ids
    q += " ORDER BY a.date DESC, a.time DESC"
    with get_conn() as c:
        return c.execute(q, params).fetchall()


def delete_attendance(attendance_id):
    with get_conn() as c:
        return c.execute("DELETE FROM attendance WHERE id = ?", (attendance_id,)).rowcount == 1


def daily_counts(days=14):
    since = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    with get_conn() as c:
        return c.execute(
            "SELECT date, COUNT(*) AS present FROM attendance WHERE date >= ? GROUP BY date ORDER BY date",
            (since,),
        ).fetchall()


# ---------------- users ----------------
def count_users():
    with get_conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def count_admins():
    with get_conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM users WHERE role='admin'").fetchone()["n"]


def create_user(username, full_name, password_hash, role):
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO users (username, full_name, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
            (username.strip(), full_name.strip(), password_hash, role, datetime.now().isoformat(timespec="seconds")))
        return cur.lastrowid


def get_user(user_id):
    with get_conn() as c:
        return c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_username(username):
    with get_conn() as c:
        return c.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()


def list_users():
    with get_conn() as c:
        return c.execute("SELECT id, username, full_name, role, active, created_at, last_login "
                         "FROM users ORDER BY role, username").fetchall()


def update_user(user_id, **fields):
    allowed = {"full_name", "role", "active", "password_hash", "last_login"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as c:
        c.execute(f"UPDATE users SET {sets} WHERE id = ?", [*fields.values(), user_id])


def delete_user(user_id):
    with get_conn() as c:
        c.execute("DELETE FROM users WHERE id = ?", (user_id,))


# ---------------- sessions (token stored hashed, so a DB leak can't be replayed) ----------------
def _h(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id):
    token = secrets.token_urlsafe(32)
    with get_conn() as c:
        c.execute("DELETE FROM sessions WHERE expires < ?", (int(time.time()),))
        c.execute("INSERT INTO sessions (token_hash, user_id, expires) VALUES (?, ?, ?)",
                  (_h(token), user_id, int(time.time()) + config.SESSION_HOURS * 3600))
    return token


def get_session_user(token):
    """Returns the user dict (plus 'session_key') for a valid, unexpired session, else None."""
    with get_conn() as c:
        row = c.execute(
            "SELECT u.*, s.token_hash AS session_key FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires > ?", (_h(token), int(time.time()))).fetchone()
    return row


def delete_session(token):
    with get_conn() as c:
        c.execute("DELETE FROM sessions WHERE token_hash = ?", (_h(token),))


def delete_user_sessions(user_id, except_key=None):
    with get_conn() as c:
        c.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, except_key or ""))


# ---------------- audit log ----------------
def log_action(username, action, detail=""):
    with get_conn() as c:
        c.execute("INSERT INTO audit (ts, username, action, detail) VALUES (?, ?, ?, ?)",
                  (datetime.now().isoformat(timespec="seconds"), username, action, detail))


def list_audit(limit=100):
    with get_conn() as c:
        return c.execute("SELECT ts, username, action, detail FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
