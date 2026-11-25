"""API tests: auth, roles, students, attendance. Uses the fake face_recognition from conftest (no dlib needed)."""
import base64
import io
import sqlite3

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import api
import config
import database

PW = "Passw0rd!123"


def img(brightness):
    """PNG data-URL of a flat image. The fake detector turns brightness into a stable 'face'."""
    buf = io.BytesIO()
    Image.new("RGB", (120, 120), (brightness,) * 3).save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@pytest.fixture(autouse=True)
def fresh():
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    database.init_db()
    api._matcher = None
    api._streaks.clear()
    api._fails.clear()


def client():
    return TestClient(api.app)


def make_user(role, username=None):
    username = username or role
    database.create_user(username, username.title(), api.security.hash_password(PW), role)


def login(role):
    c = client()
    r = c.post("/api/auth/login", json={"username": role, "password": PW})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture
def users():
    for role in ("admin", "teacher"):
        make_user(role)


# ---------------- auth ----------------
def test_first_run_setup_only_once():
    c = client()
    assert c.get("/api/auth/state").json() == {"setup_needed": True}
    r = c.post("/api/auth/setup", json={"username": "boss", "full_name": "Boss", "password": PW})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    assert c.get("/api/auth/me").json()["username"] == "boss"          # logged in via cookie
    assert client().post("/api/auth/setup", json={"username": "x1x", "full_name": "X", "password": PW}).status_code == 403


def test_legacy_user_role_migration_preserves_users_and_related_data():
    config.DB_PATH.unlink()
    c = sqlite3.connect(config.DB_PATH)
    c.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            full_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('super_admin','admin','principal','teacher')),
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_login TEXT
        );
        CREATE TABLE sections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            class_start TEXT,
            class_end TEXT,
            late_after_min INTEGER NOT NULL DEFAULT 10,
            created_at TEXT NOT NULL
        );
        CREATE TABLE students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            roll_no TEXT,
            created_at TEXT NOT NULL,
            class_name TEXT,
            section_id INTEGER REFERENCES sections(id) ON DELETE SET NULL
        );
        CREATE TABLE encodings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            encoding BLOB NOT NULL
        );
        CREATE TABLE attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            confidence REAL,
            UNIQUE (student_id, date)
        );
        CREATE TABLE sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires INTEGER NOT NULL
        );
        CREATE TABLE teacher_sections (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, section_id)
        );
        CREATE TABLE audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            username TEXT,
            action TEXT NOT NULL,
            detail TEXT
        );
        INSERT INTO sections (id, name, created_at) VALUES (4, 'Section A', '2024-01-01');
    """)
    legacy_users = [
        (10, "old-root", "Old Root", api.security.hash_password(PW), "super_admin", 1, "2024-01-01", "2024-02-01"),
        (11, "old-teacher", "Old Teacher", api.security.hash_password(PW), "teacher", 1, "2024-01-02", None),
        (12, "old-principal", "Old Principal", api.security.hash_password(PW), "principal", 1, "2024-01-03", None),
    ]
    c.executemany(
        "INSERT INTO users (id, username, full_name, password_hash, role, active, created_at, last_login) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        legacy_users,
    )
    c.execute("INSERT INTO students (id, name, roll_no, created_at, class_name, section_id) "
              "VALUES (20, 'Student A', '01', '2024-01-01', 'Section A', 4)")
    c.execute("INSERT INTO encodings (student_id, encoding) VALUES (20, ?)",
              (bytes(128 * 8),))
    c.execute("INSERT INTO attendance (id, student_id, date, time, confidence) "
              "VALUES (30, 20, '2025-01-02', '09:00:00', 0.9)")
    c.execute("INSERT INTO teacher_sections (user_id, section_id) VALUES (12, 4)")
    c.commit()
    c.close()

    token = database.create_session(10)
    database.init_db()

    root = database.get_user_by_username("old-root")
    old_principal = database.get_user_by_username("old-principal")
    assert (root["id"], root["role"], root["password_hash"], root["last_login"]) == (
        10, "admin", legacy_users[0][3], "2024-02-01")
    assert (old_principal["id"], old_principal["role"], old_principal["password_hash"]) == (
        12, "teacher", legacy_users[2][3])
    assert database.get_session_user(token)["role"] == "admin"
    assert database.teacher_section_ids(12) == [4]
    assert len(database.list_sections()) == 1
    assert len(database.list_students()) == 1
    assert database.load_all_encodings()[1] == [20]
    attendance = database.get_attendance()
    assert len(attendance) == 1 and attendance[0]["teacher_id"] is None
    c = sqlite3.connect(config.DB_PATH)
    try:
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        assert c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    finally:
        c.close()
    with pytest.raises(sqlite3.IntegrityError):
        database.create_user("second-admin", "Second Admin", api.security.hash_password(PW), "admin")


def test_login_logout_and_protected_routes(users):
    assert client().get("/api/students").status_code == 401
    assert client().post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    c = login("admin")
    assert c.get("/api/students").status_code == 200
    c.post("/api/auth/logout")
    assert c.get("/api/students").status_code == 401


def test_login_rate_limit(users):
    c = client()
    for _ in range(5):
        c.post("/api/auth/login", json={"username": "admin", "password": "bad"})
    assert c.post("/api/auth/login", json={"username": "admin", "password": PW}).status_code == 429


def test_disabled_user_cannot_login_and_is_kicked_out(users):
    c = login("teacher")
    admin = login("admin")
    tid = database.get_user_by_username("teacher")["id"]
    assert admin.patch(f"/api/users/{tid}", json={"active": False}).status_code == 200
    assert c.get("/api/auth/me").status_code == 401
    assert client().post("/api/auth/login", json={"username": "teacher", "password": PW}).status_code == 401


def test_change_password_logs_out_other_sessions(users):
    a, b = login("teacher"), login("teacher")
    assert a.post("/api/auth/change-password", json={"current": PW, "new": "NewPassw0rd!"}).status_code == 200
    assert a.get("/api/auth/me").status_code == 200
    assert b.get("/api/auth/me").status_code == 401


# ---------------- roles ----------------
def test_role_permissions(users):
    student = {"name": "Zed", "images": [img(100)]}
    assert login("teacher").post("/api/students/register", json=student).status_code == 403
    assert login("admin").post("/api/students/register", json=student).status_code == 201

    scan = {"image": img(100), "mode": "photo"}
    assert login("teacher").post("/api/attendance/scan", json=scan).status_code == 403   # no section assigned yet
    assert login("admin").post("/api/attendance/scan", json=scan).status_code == 200

    assert login("teacher").get("/api/users").status_code == 403
    assert login("teacher").get("/api/audit").status_code == 403
    assert login("admin").put("/api/settings", json={"institute_name": "X", "threshold": 0.5,
                                                    "late_after": "", "confirm_frames": 3}).status_code == 200
    assert login("admin").get("/api/audit").status_code == 200
    for role in ("teacher", "admin"):
        assert login(role).get("/api/dashboard").status_code == 200


def test_admin_can_create_teachers_but_not_another_admin(users):
    new = {"username": "newone", "full_name": "New One", "password": PW}
    admin = login("admin")
    assert admin.get("/api/users").json()["assignable_roles"] == ["teacher"]
    assert admin.post("/api/users", json={**new, "role": "admin"}).status_code == 403
    assert admin.post("/api/users", json={**new, "role": "super_admin"}).status_code == 422
    assert admin.post("/api/users", json={**new, "role": "principal"}).status_code == 422
    assert admin.post("/api/users", json={**new, "role": "teacher"}).status_code == 201
    assert admin.post("/api/users", json={**new, "role": "teacher"}).status_code == 409  # duplicate username
    with pytest.raises(sqlite3.IntegrityError):
        database.create_user("another_admin", "Another Admin", api.security.hash_password(PW), "admin")


def test_cannot_lock_yourself_out(users):
    admin = login("admin")
    me = admin.get("/api/auth/me").json()["id"]
    assert admin.patch(f"/api/users/{me}", json={"role": "teacher"}).status_code == 403
    assert admin.patch(f"/api/users/{me}", json={"active": False}).status_code == 403
    assert admin.delete(f"/api/users/{me}").status_code == 403


def test_weak_password_rejected(users):
    r = login("admin").post("/api/users", json={"username": "weak1", "full_name": "W", "password": "123", "role": "teacher"})
    assert r.status_code == 422


# ---------------- students + attendance ----------------
def test_register_scan_and_records(users):
    admin, teacher = login("admin"), login("teacher")
    tid = database.get_user_by_username("teacher")["id"]
    sec = admin.post("/api/sections", json={"name": "BSCS-6", "teacher_ids": [tid]}).json()["id"]
    r = admin.post("/api/students/register", json={"name": "Adeela", "roll_no": "CS-1", "section_id": sec,
                                                  "images": [img(100), img(100)]})
    assert r.status_code == 201 and r.json()["added"] == 2
    assert admin.post("/api/students/register", json={"name": "adeela", "images": [img(100)]}).status_code == 409

    # unknown face is not marked
    res = teacher.post("/api/attendance/scan", json={"image": img(200), "section_id": sec}).json()
    assert res["faces"][0]["state"] == "unknown" and res["marked"] == []

    # live mode needs 3 consecutive scans (default confirm_frames)
    states = [teacher.post("/api/attendance/scan", json={"image": img(100), "section_id": sec}).json()["faces"][0]["state"] for _ in range(3)]
    assert states == ["confirming", "confirming", "marked"]
    assert teacher.post("/api/attendance/scan", json={"image": img(100), "section_id": sec}).json()["faces"][0]["state"] == "already"

    rows = admin.get("/api/attendance").json()["records"]
    assert len(rows) == 1 and rows[0]["name"] == "Adeela" and rows[0]["class_name"] == "BSCS-6"
    assert admin.get("/api/attendance", params={"class_name": "other"}).json()["records"] == []
    assert admin.get("/api/attendance", params={"start": "bad"}).status_code == 422

    d = admin.get("/api/dashboard").json()
    assert (d["total"], d["present"], d["absent"], d["rate"]) == (1, 1, 0, 100)
    assert len(d["series"]) == 14 and d["series"][-1]["present"] == 1


def test_photo_mode_marks_immediately_and_preview_does_not(users):
    admin, another_session = login("admin"), login("admin")
    admin.post("/api/students/register", json={"name": "Sara", "images": [img(90)]})
    assert another_session.post("/api/attendance/scan", json={"image": img(90), "mode": "photo", "mark": False}).json()["marked"] == []
    assert another_session.post("/api/attendance/scan", json={"image": img(90), "mode": "photo"}).json()["marked"] == ["Sara"]


def test_late_status_follows_setting(users):
    admin = login("admin")
    admin.post("/api/students/register", json={"name": "Hina", "images": [img(80)]})
    admin.put("/api/settings", json={"institute_name": "Test", "threshold": 0.5, "late_after": "00:00", "confirm_frames": 1})
    admin.post("/api/attendance/scan", json={"image": img(80)})
    assert admin.get("/api/attendance").json()["records"][0]["status"] == "late"
    assert admin.get("/api/dashboard").json()["late"] == 1


def test_manual_attendance_delete_and_csv_injection(users):
    admin, teacher = login("admin"), login("teacher")
    sid = admin.post("/api/students/register", json={"name": "=HYPERLINK(1)", "images": [img(70)]}).json()["student"]["id"]
    assert teacher.post("/api/attendance/manual", json={"student_id": sid}).status_code == 403
    assert admin.post("/api/attendance/manual", json={"student_id": sid}).status_code == 201
    assert admin.post("/api/attendance/manual", json={"student_id": sid}).status_code == 409
    csv_text = admin.get("/api/attendance/export.csv").text
    assert "'=HYPERLINK(1)" in csv_text and csv_text.startswith("Name,Roll no,Class,Date,Time,Status")
    aid = admin.get("/api/attendance").json()["records"][0]["id"]
    assert admin.delete(f"/api/attendance/{aid}").status_code == 200
    assert admin.get("/api/attendance").json()["records"] == []


def test_manual_attendance_accepts_custom_date_and_time(users):
    admin, teacher = login("admin"), login("teacher")
    sid = admin.post("/api/students/register", json={"name": "Session Student", "images": [img(71)]}).json()["student"]["id"]
    r = admin.post("/api/attendance/manual", json={
        "student_id": sid, "day": "2026-02-10", "at": "09:45", "status": "present"
    })
    assert r.status_code == 201
    record = admin.get("/api/attendance", params={"start": "2026-02-10", "end": "2026-02-10"}).json()["records"][0]
    assert record["date"] == "2026-02-10"
    assert record["time"].startswith("09:45:")


def test_teacher_attendance_is_section_scoped_and_only_own_records_are_deletable(users):
    admin, teacher = login("admin"), login("teacher")
    other_created = admin.post("/api/users", json={
        "username": "other_teacher",
        "full_name": "Other Teacher",
        "password": PW,
        "role": "teacher",
    })
    assert other_created.status_code == 201
    new_section_teacher = admin.post("/api/users", json={
        "username": "new_section_teacher",
        "full_name": "New Section Teacher",
        "password": PW,
        "role": "teacher",
    })
    assert new_section_teacher.status_code == 201
    teacher_id = database.get_user_by_username("teacher")["id"]
    other_teacher_id = database.get_user_by_username("other_teacher")["id"]
    new_section_teacher_id = database.get_user_by_username("new_section_teacher")["id"]
    own_section = admin.post("/api/sections", json={
        "name": "Shared Section",
        "teacher_ids": [teacher_id, other_teacher_id],
    }).json()["id"]
    other_section = admin.post("/api/sections", json={
        "name": "Other Section",
        "teacher_ids": [other_teacher_id],
    }).json()["id"]
    new_section = admin.post("/api/sections", json={
        "name": "New Section",
        "teacher_ids": [new_section_teacher_id],
    }).json()["id"]
    own_student = database.add_student("Own Section Student", section_id=own_section)
    other_student = database.add_student("Other Section Student", section_id=other_section)

    database.mark_attendance_ex(
        own_student, status="present", day="2025-01-01", at="09:00:00",
        section_id=own_section, teacher_id=None,
    )
    database.mark_attendance_ex(
        own_student, status="present", day="2025-01-02", at="09:00:00",
        section_id=own_section, teacher_id=other_teacher_id,
    )
    database.mark_attendance_ex(
        other_student, status="present", day="2025-01-03", at="09:00:00",
        section_id=other_section, teacher_id=other_teacher_id,
    )
    own_mark = teacher.post("/api/attendance/manual", json={
        "student_id": own_student,
        "day": "2025-01-04",
        "at": "09:03",
        "status": "present",
    })
    assert own_mark.status_code == 201
    assert teacher.post("/api/attendance/manual", json={
        "student_id": other_student, "day": "2025-01-05", "at": "09:00",
    }).status_code == 403

    teacher_rows = teacher.get("/api/attendance").json()["records"]
    assert len(teacher_rows) == 3
    assert {row["name"] for row in teacher_rows} == {"Own Section Student"}
    assert {row["teacher_id"] for row in teacher_rows} == {None, teacher_id, other_teacher_id}
    own_record = next(row for row in teacher_rows if row["teacher_id"] == teacher_id)
    historical = next(row for row in teacher_rows if row["teacher_id"] is None)
    other_owned = next(row for row in teacher_rows if row["teacher_id"] == other_teacher_id)
    assert own_record["date"] == "2025-01-04" and own_record["time"].startswith("09:03:")
    assert teacher.delete(f"/api/attendance/{historical['id']}").status_code == 403
    assert teacher.delete(f"/api/attendance/{other_owned['id']}").status_code == 403
    assert teacher.delete(f"/api/attendance/{own_record['id']}").status_code == 200

    assert admin.patch(f"/api/students/{own_student}", json={
        "name": "Own Section Student",
        "roll_no": "01",
        "section_id": new_section,
    }).status_code == 200
    new_teacher = login("new_section_teacher")
    assert new_teacher.get("/api/attendance").json()["records"] == []

    admin_rows = admin.get("/api/attendance").json()["records"]
    assert len(admin_rows) == 3
    assert {row["name"] for row in admin_rows} == {"Own Section Student", "Other Section Student"}
    assert next(row for row in admin_rows if row["id"] == historical["id"])["class_name"] == "Shared Section"
    assert admin.delete(f"/api/attendance/{historical['id']}").status_code == 200


def test_admin_creates_teacher_with_sections_and_teacher_login_isolated(users):
    admin = login("admin")
    sec1 = admin.post("/api/sections", json={"name": "Teacher Section 1"}).json()["id"]
    sec2 = admin.post("/api/sections", json={"name": "Teacher Section 2"}).json()["id"]

    created = admin.post("/api/users", json={
        "username": "teacher1",
        "full_name": "Teacher One",
        "password": PW,
        "role": "teacher",
        "section_ids": [sec1],
    })
    assert created.status_code == 201

    created = admin.post("/api/users", json={
        "username": "teacher_separate",
        "full_name": "Teacher Separate",
        "password": PW,
        "role": "teacher",
        "section_ids": [sec1, sec2],
    })
    assert created.status_code == 201
    student1 = admin.post("/api/students/register", json={
        "name": "Student One",
        "section_id": sec1,
        "images": [img(61)],
    }).json()["student"]
    student2 = admin.post("/api/students/register", json={
        "name": "Student Two",
        "section_id": sec2,
        "images": [img(62)],
    }).json()["student"]

    teacher = client()
    assert teacher.post("/api/auth/login", json={
        "username": "teacher_separate",
        "password": PW,
    }).status_code == 200
    assert [s["name"] for s in teacher.get("/api/sections").json()["sections"]] == [
        "Teacher Section 1", "Teacher Section 2"]
    assert {s["id"] for s in teacher.get("/api/students").json()["students"]} == {
        student1["id"], student2["id"]}
    assert [s["id"] for s in teacher.get("/api/students", params={"section_id": sec1}).json()["students"]] == [student1["id"]]
    assert [s["id"] for s in teacher.get("/api/students", params={"section_id": sec2}).json()["students"]] == [student2["id"]]


def test_custom_session_duplicate_is_prevented_for_teacher(users):
    admin = login("admin")
    tid = database.get_user_by_username("teacher")["id"]
    sec = admin.post("/api/sections", json={"name": "Session Section", "teacher_ids": [tid]}).json()["id"]
    sid = admin.post("/api/students/register", json={
        "name": "Session Duplicate",
        "section_id": sec,
        "images": [img(82)],
    }).json()["student"]["id"]

    teacher = login("teacher")
    payload = {
        "student_id": sid,
        "day": "2026-03-12",
        "at": "10:30",
        "status": "present",
    }
    assert teacher.post("/api/attendance/manual", json=payload).status_code == 201
    assert teacher.post("/api/attendance/manual", json=payload).status_code == 409


def test_unusable_photos_do_not_leave_empty_student(users):
    r = login("admin").post("/api/students/register", json={"name": "Ghost", "images": ["data:image/png;base64,AAAA"]})
    assert r.status_code == 422
    assert database.get_student_by_name("Ghost") is None


def test_delete_student_removes_avatar_and_face_data(users):
    admin = login("admin")
    sid = admin.post("/api/students/register", json={"name": "Temp", "images": [img(60)]}).json()["student"]["id"]
    assert admin.get(f"/api/students/{sid}/avatar").status_code == 200
    assert admin.delete(f"/api/students/{sid}").status_code == 200
    assert admin.get(f"/api/students/{sid}/avatar").status_code == 404
    assert database.load_all_encodings()[1] == []


def test_settings_validation_and_audit(users):
    admin = login("admin")
    bad = {"institute_name": "X", "threshold": 0.9, "late_after": "25:00", "confirm_frames": 3}
    assert admin.put("/api/settings", json=bad).status_code == 422
    ok = {"institute_name": "JUW", "threshold": 0.45, "late_after": "08:30", "confirm_frames": 4}
    assert admin.put("/api/settings", json=ok).json() == {**ok, "early_minutes": 30}
    assert any(e["action"] == "settings_changed" for e in admin.get("/api/audit").json()["entries"])


def test_pages_served_and_docs_hidden():
    c = client()
    for p in ("/", "/login", "/attendance", "/students", "/register", "/records", "/users", "/sections", "/settings"):
        assert c.get(p).status_code == 200, p
    assert c.get("/docs").status_code == 404
    assert c.get("/static/app.js").status_code == 200


# ---------------- sections, roll nos, teacher scope, class-time late rule ----------------
def test_roll_numbers_are_assigned_automatically(users):
    admin = login("admin")
    sec = admin.post("/api/sections", json={"name": "A"}).json()["id"]
    sec2 = admin.post("/api/sections", json={"name": "B"}).json()["id"]
    ids = [admin.post("/api/students/register", json={"name": n, "section_id": sec, "images": [img(30 + i * 40)]}).json()["student"]
           for i, n in enumerate(("Zara", "Amna", "Bilal"))]
    assert [s["roll_no"] for s in ids] == ["01", "02", "03"]
    # students added without a section (e.g. import_known_faces) get roll nos when moved in, alphabetically
    free = [database.add_student(n) for n in ("Sana", "Hira")]
    assert admin.post("/api/students/assign-section", json={"student_ids": free, "section_id": sec2}).json() == {"moved": 2}
    assert {s["name"]: s["roll_no"] for s in database.list_students(section_id=sec2)} == {"Hira": "01", "Sana": "02"}
    # manual duplicate roll no in the same section is refused, empty roll no on edit gets auto
    sid = ids[0]["id"]
    assert admin.patch(f"/api/students/{sid}", json={"name": "Zara", "roll_no": "02", "section_id": sec}).status_code == 409
    assert admin.patch(f"/api/students/{sid}", json={"name": "Zara", "roll_no": "", "section_id": sec2}).json()["roll_no"] == "03"


def test_legacy_class_names_migrate_to_sections():
    import sqlite3
    config.DB_PATH.unlink()
    con = sqlite3.connect(config.DB_PATH)
    con.executescript("CREATE TABLE students (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, roll_no TEXT,"
                      " created_at TEXT NOT NULL, class_name TEXT);"
                      "INSERT INTO students (name, created_at, class_name) VALUES ('B','x','BSCS-6'),('A','x','BSCS-6'),('C','x',NULL);")
    con.commit(); con.close()
    database.init_db(); database.init_db()                    # idempotent
    rows = {s["name"]: s for s in database.list_students()}
    assert rows["A"]["class_name"] == "BSCS-6" and (rows["A"]["roll_no"], rows["B"]["roll_no"]) == ("01", "02")
    assert rows["C"]["section_id"] is None and rows["C"]["roll_no"] is None
    assert len(database.list_sections()) == 1


def test_late_follows_class_time():
    from datetime import datetime
    sec = database.create_section("X", "10:00", "11:00", 10)
    st = database.get_student(database.add_student("Ali", section_id=sec))
    at = lambda h, m: datetime(2026, 1, 5, h, m, 30)
    assert database.status_for_student(st, at(10, 5))[0] == "present"
    assert database.status_for_student(st, at(9, 45))[0] == "present"        # opens 30 min early
    assert database.status_for_student(st, at(10, 50))[0] == "late"           # the 10-11 class example
    assert database.status_for_student(st, at(9, 20))[0] == "closed"
    assert database.status_for_student(st, at(11, 1))[0] == "closed"
    nosec = database.get_student(database.add_student("NoSec"))
    assert database.status_for_student(nosec, at(23, 0))[1] is None           # falls back to global late-after


def test_scan_outside_class_time_saves_nothing(users):
    admin, teacher = login("admin"), login("teacher")
    sec = admin.post("/api/sections", json={"name": "Z", "class_start": "00:00", "class_end": "00:01"}).json()["id"]
    admin.post("/api/students/register", json={"name": "Late Larry", "section_id": sec, "images": [img(85)]})
    r = admin.post("/api/attendance/scan", json={"image": img(85), "mode": "photo", "section_id": sec}).json()
    from datetime import datetime
    if not datetime.now().strftime("%H:%M") <= "00:01":
        assert r["faces"][0]["state"] == "closed" and r["marked"] == []
        assert admin.get("/api/attendance").json()["records"] == []


def test_section_validation(users):
    admin = login("admin")
    assert admin.post("/api/sections", json={"name": "S", "class_start": "10:00"}).status_code == 422        # only one time
    assert admin.post("/api/sections", json={"name": "S", "class_start": "11:00", "class_end": "10:00"}).status_code == 422
    assert admin.post("/api/sections", json={"name": "S", "class_start": "10:00", "class_end": "11:00"}).status_code == 201
    assert admin.post("/api/sections", json={"name": "s"}).status_code == 409
    tid = database.get_user_by_username("admin")["id"]
    assert admin.post("/api/sections", json={"name": "T", "teacher_ids": [tid]}).status_code == 422          # not a teacher
    assert login("teacher").post("/api/sections", json={"name": "Q"}).status_code == 403


def test_teacher_only_sees_and_scans_assigned_sections(users):
    admin, teacher = login("admin"), login("teacher")
    tid = database.get_user_by_username("teacher")["id"]
    mine = admin.post("/api/sections", json={"name": "Mine", "teacher_ids": [tid]}).json()["id"]
    other = admin.post("/api/sections", json={"name": "Other"}).json()["id"]
    admin.post("/api/students/register", json={"name": "Mine S", "section_id": mine, "images": [img(50)]})
    admin.post("/api/students/register", json={"name": "Other S", "section_id": other, "images": [img(150)]})

    assert [s["name"] for s in teacher.get("/api/sections").json()["sections"]] == ["Mine"]
    assert [s["name"] for s in teacher.get("/api/students").json()["students"]] == ["Mine S"]
    assert teacher.get("/api/students", params={"section_id": other}).status_code == 403
    assert len(admin.get("/api/students").json()["students"]) == 2
    assert teacher.get("/api/dashboard").json()["total"] == 1

    scan = lambda img_, **k: teacher.post("/api/attendance/scan", json={"image": img_, "mode": "photo", **k})
    assert scan(img(50)).json()["faces"][0]["state"] == "marked"              # single section -> picked automatically
    assert scan(img(50), section_id=other).status_code == 403
    r = scan(img(150), section_id=mine).json()["faces"][0]
    assert r["state"] == "other_section" and r["name"] == "Student from another section"
    assert [x["name"] for x in teacher.get("/api/attendance").json()["records"]] == ["Mine S"]
    assert "Other S" not in teacher.get("/api/attendance/export.csv").text

    admin.delete(f"/api/sections/{mine}")                                    # students stay, become unassigned
    assert database.get_student_by_name("Mine S")["section_id"] is None


def test_teacher_with_multiple_sections_must_select_assigned_section(users):
    admin, teacher = login("admin"), login("teacher")
    tid = database.get_user_by_username("teacher")["id"]
    mine1 = admin.post("/api/sections", json={"name": "Mine 1", "teacher_ids": [tid]}).json()["id"]
    mine2 = admin.post("/api/sections", json={"name": "Mine 2", "teacher_ids": [tid]}).json()["id"]
    other = admin.post("/api/sections", json={"name": "Not Assigned"}).json()["id"]

    mine1_students = [s["name"] for s in teacher.get("/api/students", params={"section_id": mine1}).json()["students"]]
    mine2_students = [s["name"] for s in teacher.get("/api/students", params={"section_id": mine2}).json()["students"]]
    assert mine1_students == []
    assert mine2_students == []
    assert teacher.get("/api/students", params={"section_id": other}).status_code == 403
    assert teacher.post("/api/attendance/scan", json={"image": img(20), "mode": "photo"}).status_code == 400


def test_teacher_without_section_cannot_scan(users):
    r = login("teacher").post("/api/attendance/scan", json={"image": img(10), "mode": "photo"})
    assert r.status_code == 403


# ================= regression tests for the role / records / users / landing work =================
def _snapshot(table):
    c = sqlite3.connect(config.DB_PATH)
    try:
        return c.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
    finally:
        c.close()


def test_only_two_roles_and_admin_has_full_access(users):
    assert api.security.ROLES == ("admin", "teacher")
    admin = login("admin")
    assert admin.get("/api/auth/me").json()["role_label"] == "Admin"
    can = admin.get("/api/auth/me").json()["can"]
    assert all(can.values())                                   # full access
    assert admin.get("/api/users").json()["assignable_roles"] == ["teacher"]
    with sqlite3.connect(config.DB_PATH) as c:
        with pytest.raises(sqlite3.IntegrityError):            # DB itself refuses any other role
            c.execute("INSERT INTO users (username, full_name, password_hash, role, created_at) "
                      "VALUES ('x', 'x', 'x', 'super_admin', 'now')")


def test_startup_does_not_rebuild_or_change_attendance_and_never_assigns_owner(users):
    admin = login("admin")
    sec = admin.post("/api/sections", json={"name": "Hist"}).json()["id"]
    sid = admin.post("/api/students/register", json={"name": "Hist Student", "section_id": sec,
                                                     "images": [img(70)]}).json()["student"]["id"]
    with sqlite3.connect(config.DB_PATH) as c:                  # historical row written before teacher_id existed
        c.execute("INSERT INTO attendance (student_id, date, time, status, section_id, teacher_id) "
                  "VALUES (?, '2026-01-05', '09:00:00', 'late', ?, NULL)", (sid, sec))
    before_att, before_enc, before_users = _snapshot("attendance"), _snapshot("encodings"), _snapshot("users")

    traced = []
    real_connect = sqlite3.connect

    def tracing_connect(*a, **k):
        con = real_connect(*a, **k)
        con.set_trace_callback(traced.append)
        return con

    sqlite3.connect = tracing_connect
    try:
        database.init_db()
        database.init_db()
    finally:
        sqlite3.connect = real_connect

    assert not any("attendance_legacy" in s for s in traced)    # the table is not renamed / rebuilt on start-up
    assert _snapshot("attendance") == before_att                # identical rows, NULL teacher_id still NULL
    assert _snapshot("encodings") == before_enc                 # face encodings untouched
    assert _snapshot("users") == before_users                   # accounts + password hashes untouched


def test_legacy_attendance_table_migrates_atomically_and_keeps_every_row():
    config.DB_PATH.unlink()
    c = sqlite3.connect(config.DB_PATH)
    c.executescript("""
        CREATE TABLE students (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, roll_no TEXT,
                               created_at TEXT NOT NULL);
        CREATE TABLE attendance (id INTEGER PRIMARY KEY AUTOINCREMENT,
                                 student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                                 date TEXT NOT NULL, time TEXT NOT NULL, confidence REAL, UNIQUE(student_id, date));
        INSERT INTO students VALUES (1, 'a', '1', 'x'), (2, 'b', '2', 'x');
        INSERT INTO attendance (id, student_id, date, time, confidence)
            VALUES (5, 1, '2026-01-01', '09:00:00', 0.3), (9, 2, '2026-01-01', '09:05:00', NULL);
    """)
    c.close()
    database.init_db()
    database.init_db()                                           # second start must be a no-op
    rows = database.get_attendance()
    assert sorted((r["id"], r["student_id"], r["teacher_id"]) for r in rows) == [(5, 1, None), (9, 2, None)]
    with sqlite3.connect(config.DB_PATH) as c:
        assert c.execute("SELECT name FROM sqlite_master WHERE name='attendance_legacy'").fetchall() == []


def test_historical_null_teacher_records_visibility_and_edit_rules(users):
    admin, teacher = login("admin"), login("teacher")
    tid = database.get_user_by_username("teacher")["id"]
    mine = admin.post("/api/sections", json={"name": "Mine", "teacher_ids": [tid]}).json()["id"]
    other = admin.post("/api/sections", json={"name": "Other"}).json()["id"]
    s_mine = admin.post("/api/students/register", json={"name": "Mine Kid", "section_id": mine, "images": [img(81)]}).json()["student"]["id"]
    s_other = admin.post("/api/students/register", json={"name": "Other Kid", "section_id": other, "images": [img(82)]}).json()["student"]["id"]
    with sqlite3.connect(config.DB_PATH) as c:
        ids = {}
        for key, (st, sec) in {"mine": (s_mine, mine), "other": (s_other, other)}.items():
            ids[key] = c.execute("INSERT INTO attendance (student_id, date, time, status, section_id, teacher_id) "
                                 "VALUES (?, '2026-01-05', '09:00:00', 'late', ?, NULL)", (st, sec)).lastrowid
    # Admin sees everything
    assert {r["id"] for r in admin.get("/api/attendance").json()["records"]} == set(ids.values())
    # Teacher sees only the record in an assigned section; ownership stays NULL (never guessed)
    seen = teacher.get("/api/attendance").json()["records"]
    assert [r["id"] for r in seen] == [ids["mine"]] and seen[0]["teacher_id"] is None
    # Teacher cannot modify a record they did not create (legacy NULL owner), nor one outside their sections
    assert teacher.delete(f"/api/attendance/{ids['mine']}").status_code == 403
    assert teacher.delete(f"/api/attendance/{ids['other']}").status_code == 403
    assert {r["id"] for r in admin.get("/api/attendance").json()["records"]} == set(ids.values())
    # Admin can
    assert admin.delete(f"/api/attendance/{ids['mine']}").status_code == 200


def test_records_dropdown_students_for_teacher_with_multiple_sections(users):
    admin, teacher = login("admin"), login("teacher")
    tid = database.get_user_by_username("teacher")["id"]
    a = admin.post("/api/sections", json={"name": "Multi A", "teacher_ids": [tid]}).json()["id"]
    b = admin.post("/api/sections", json={"name": "Multi B", "teacher_ids": [tid]}).json()["id"]
    x = admin.post("/api/sections", json={"name": "Not Mine"}).json()["id"]
    ids = {n: admin.post("/api/students/register", json={"name": n, "section_id": s, "images": [img(i)]}).json()["student"]["id"]
           for n, s, i in (("Kid A", a, 91), ("Kid B", b, 92), ("Kid X", x, 93))}
    data = teacher.get("/api/students").json()                   # what the Records page loads
    assert {s["name"] for s in data["students"]} == {"Kid A", "Kid B"}
    assert all(s["class_name"] in ("Multi A", "Multi B") for s in data["students"])
    assert sorted(data["classes"]) == ["Multi A", "Multi B"]
    # manual entry works for students of either assigned section, and is stored under the teacher
    for kid in ("Kid A", "Kid B"):
        assert teacher.post("/api/attendance/manual", json={"student_id": ids[kid]}).status_code == 201
    assert teacher.post("/api/attendance/manual", json={"student_id": ids["Kid X"]}).status_code == 403
    assert {r["teacher_id"] for r in teacher.get("/api/attendance").json()["records"]} == {tid}


def test_users_create_and_update_report_the_sections_that_were_really_saved(users):
    admin = login("admin")
    s1 = admin.post("/api/sections", json={"name": "U1"}).json()["id"]
    s2 = admin.post("/api/sections", json={"name": "U2"}).json()["id"]
    new = {"username": "tea_two", "full_name": "Tea Two", "password": PW, "role": "teacher"}
    r = admin.post("/api/users", json={**new, "section_ids": [s1, s2]})
    assert r.status_code == 201 and sorted(r.json()["sections"]) == ["U1", "U2"]
    uid = r.json()["id"]
    assert sorted(database.teacher_section_ids(uid)) == [s1, s2]
    r = admin.patch(f"/api/users/{uid}", json={"section_ids": [s2]})
    assert r.status_code == 200 and r.json()["sections"] == ["U2"]
    r = admin.patch(f"/api/users/{uid}", json={"section_ids": []})
    assert r.status_code == 200 and r.json()["sections"] == []   # lets the page warn "no section assigned"
    r = admin.post("/api/users", json={**new, "username": "tea_none"})
    assert r.status_code == 201 and r.json()["sections"] == []
    assert admin.post("/api/users", json={**new, "username": "tea_bad", "section_ids": [9999]}).status_code == 404
    assert database.get_user_by_username("tea_bad") is None      # nothing half-created


def test_landing_page_for_visitors_and_dashboard_for_logged_in_users(users):
    visitor = client().get("/")
    assert visitor.status_code == 200
    html = visitor.text
    assert 'href="/login"' in html and "Try Demo" in html and "Features" in html
    assert "fetch(" not in html and "/api/" not in html          # demo is static: no backend calls
    admin = login("admin")
    dash = admin.get("/")
    assert dash.status_code == 200 and "Dashboard" in dash.text and "Try Demo" not in dash.text
    assert client().get("/static/landing.css").status_code == 200
