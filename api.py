"""Web backend + static frontend.   Run:  uvicorn api:app --reload   ->  http://localhost:8000"""
import base64
import binascii
import csv
import io
import shutil
import threading
import time
from datetime import date, datetime, timedelta, time as TimeValue
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

import config
import database
import face_engine
import security

database.init_db()

app = FastAPI(
    title="Face Attendance",
    docs_url="/docs" if config.DEBUG else None,
    redoc_url=None,
    openapi_url="/openapi.json" if config.DEBUG else None,
)


@app.get("/api/health")
def health_check():
    """Basic runtime diagnostics; does not expose user or student data."""
    try:
        face_engine._fr()
        engine = "ready"
        engine_error = None
    except Exception as exc:
        engine = "unavailable"
        engine_error = f"{type(exc).__name__}: {exc}"
    return {
        "status": "ok",
        "frontend": (config.BASE_DIR / "static" / "index.html").is_file(),
        "database": config.DB_PATH.is_file(),
        "face_engine": engine,
        "face_engine_error": engine_error,
        "camera": "browser-managed; open this app on localhost or HTTPS",
    }
STATIC = config.BASE_DIR / "static"
COOKIE = "session"


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "camera=(self), microphone=()"
    if not request.url.path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# =============================================================== auth helpers
def current_user(request: Request):
    token = request.cookies.get(COOKIE)
    user = database.get_session_user(token) if token else None
    if not user or not user["active"]:
        raise HTTPException(401, "Please log in first")
    return user


def require(perm):
    def dep(user=Depends(current_user)):
        if user["role"] not in security.PERMS[perm]:
            raise HTTPException(403, "You do not have permission for this action")
        return user
    return dep


def allowed_sections(user):
    """None = may see every section. Teachers only see the sections they are assigned to (a set, maybe empty)."""
    if user["role"] == "teacher":
        return set(database.teacher_section_ids(user["id"]))
    return None


def audit(user, action, detail=""):
    database.log_action(user["username"] if user else None, action, detail)


def public_user(u):
    role = u["role"]
    return {
        "id": u["id"], "username": u["username"], "full_name": u["full_name"], "role": role,
        "role_label": security.ROLE_LABEL[role],
        "can": {
            "take_attendance": role in security.PERMS["attendance.take"],
            "edit_attendance": role in security.PERMS["attendance.edit"],
            "manage_students": role in security.PERMS["students.manage"],
            "manage_sections": role in security.PERMS["sections.manage"],
            "manage_users": role in security.PERMS["users.manage"],
            "edit_settings": role in security.PERMS["settings.edit"],
            "view_audit": role in security.PERMS["audit.view"],
        },
    }


# =============================================================== models
def Name():
    return Field(min_length=1, max_length=80)

def Password():
    return Field(min_length=8, max_length=128)

def Username():
    return Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9_.-]+$")

def Image():
    return Field(max_length=12_000_000)



class LoginIn(BaseModel):
    username: str = Field(max_length=80)
    password: str = Field(max_length=128)


class SetupIn(BaseModel):
    username: str = Username()
    full_name: str = Name()
    password: str = Password()


class ChangePasswordIn(BaseModel):
    current: str = Field(max_length=128)
    new: str = Password()


class UserIn(SetupIn):
    role: Literal["admin", "teacher"]
    section_ids: list[int] = Field(default_factory=list, max_length=100)


class UserPatch(BaseModel):
    full_name: Optional[str] = Field(None, min_length=1, max_length=80)
    role: Optional[Literal["admin", "teacher"]] = None
    active: Optional[bool] = None
    section_ids: Optional[list[int]] = Field(None, max_length=100)


class ResetPasswordIn(BaseModel):
    password: str = Password()


TIME_RE = r"^(([01]\d|2[0-3]):[0-5]\d)?$"


class StudentIn(BaseModel):
    name: str = Name()
    roll_no: Optional[str] = Field(None, max_length=30)      # empty = auto (01, 02, ...) when a section is set
    section_id: Optional[int] = None


class SectionIn(BaseModel):
    name: str = Name()
    class_start: str = Field("", pattern=TIME_RE)           # "" = no class time (global Late-after is used)
    class_end: str = Field("", pattern=TIME_RE)
    late_after_min: int = Field(10, ge=0, le=180)
    teacher_ids: list[int] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def _times(self):
        if bool(self.class_start) != bool(self.class_end):
            raise ValueError("Provide both class start and end times, or leave both empty")
        if self.class_start and self.class_end <= self.class_start:
            raise ValueError("Class end time must be after the class start time")
        return self


class AssignSectionIn(BaseModel):
    student_ids: list[int] = Field(min_length=1, max_length=1000)
    section_id: Optional[int] = None


class RegisterIn(StudentIn):
    images: list[str] = Field(min_length=1, max_length=10)


class PhotosIn(BaseModel):
    images: list[str] = Field(min_length=1, max_length=10)


class ScanIn(BaseModel):
    image: str = Image()
    mode: Literal["live", "photo"] = "live"
    mark: bool = True
    section_id: Optional[int] = None      # which section's class is being taken (required for teachers)
    day: Optional[date] = None
    at: Optional[TimeValue] = None


class ManualIn(BaseModel):
    student_id: int
    day: Optional[date] = None
    at: Optional[TimeValue] = None
    status: Literal["present", "late"] = "present"


class SettingsIn(BaseModel):
    institute_name: str = Field(min_length=1, max_length=80)
    threshold: float = Field(ge=0.3, le=0.7)
    late_after: str = Field("", pattern=r"^(([01]\d|2[0-3]):[0-5]\d)?$")
    confirm_frames: int = Field(ge=1, le=10)
    early_minutes: int = Field(30, ge=0, le=240)


# =============================================================== auth routes
_fails = {}
_fails_lock = threading.Lock()


def _too_many_failures(key):
    with _fails_lock:
        recent = [t for t in _fails.get(key, []) if time.time() - t < 900]
        _fails[key] = recent
        return len(recent) >= 5


def _start_session(response, user):
    token = database.create_session(user["id"])
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=config.COOKIE_SECURE,
                        max_age=config.SESSION_HOURS * 3600)
    database.update_user(user["id"], last_login=datetime.now().isoformat(timespec="seconds"))


@app.get("/api/auth/state")
def auth_state():
    return {"setup_needed": database.count_users() == 0}


@app.post("/api/auth/setup")
def auth_setup(body: SetupIn, response: Response):
    """First run only: create the Admin account."""
    if database.count_users() > 0:
        raise HTTPException(403, "Initial setup has already been completed")
    uid = database.create_user(body.username, body.full_name, security.hash_password(body.password), "admin")
    user = database.get_user(uid)
    _start_session(response, user)
    audit(user, "setup", "Admin created")
    return public_user(user)


@app.post("/api/auth/login")
def auth_login(body: LoginIn, request: Request, response: Response):
    key = (body.username.lower(), request.client.host if request.client else "")
    if _too_many_failures(key):
        raise HTTPException(429, "Too many failed attempts. Try again after 15 minutes.")
    user = database.get_user_by_username(body.username)
    ok = security.verify_password(body.password, user["password_hash"] if user else security.DUMMY_HASH)
    if not (user and ok and user["active"]):
        with _fails_lock:
            _fails.setdefault(key, []).append(time.time())
        audit(None, "login_failed", body.username[:40])
        raise HTTPException(401, "Incorrect username or password, or the account is disabled")
    with _fails_lock:
        _fails.pop(key, None)
    _start_session(response, user)
    audit(user, "login")
    return public_user(user)


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE)
    if token:
        database.delete_session(token)
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(user=Depends(current_user)):
    return public_user(user)


@app.post("/api/auth/change-password")
def change_password(body: ChangePasswordIn, user=Depends(current_user)):
    if not security.verify_password(body.current, user["password_hash"]):
        raise HTTPException(400, "The current password is incorrect")
    database.update_user(user["id"], password_hash=security.hash_password(body.new))
    database.delete_user_sessions(user["id"], except_key=user["session_key"])   # log out other devices
    audit(user, "password_changed")
    return {"ok": True}


# =============================================================== users
def _manageable_target(actor, uid):
    target = database.get_user(uid)
    if not target:
        raise HTTPException(404, "User not found")
    if not security.can_manage(actor["role"], target["role"]):
        raise HTTPException(403, "You do not have permission to manage this user")
    return target


def _public_row(u, sections=None):
    return {k: u[k] for k in ("id", "username", "full_name", "role", "active", "created_at", "last_login")} | \
           {"role_label": security.ROLE_LABEL[u["role"]],
            "sections": (sections if sections is not None else database.teacher_sections_map()).get(u["id"], [])}


def _validate_teacher_sections(section_ids):
    for section_id in set(section_ids):
        if not database.get_section(section_id):
            raise HTTPException(404, "Section not found")


@app.get("/api/users")
def users_list(actor=Depends(require("users.manage"))):
    rows, secmap = database.list_users(), database.teacher_sections_map()
    return {"users": [_public_row(u, secmap) | {"manageable": security.can_manage(actor["role"], u["role"]) and u["id"] != actor["id"]}
                      for u in rows],
            "assignable_roles": [r for r in security.ROLES if security.can_manage(actor["role"], r)]}


@app.post("/api/users", status_code=201)
def users_create(body: UserIn, actor=Depends(require("users.manage"))):
    if not security.can_manage(actor["role"], body.role):
        raise HTTPException(403, f"You cannot create a {security.ROLE_LABEL[body.role]} account")
    if database.get_user_by_username(body.username):
        raise HTTPException(409, "This username already exists")
    if body.role == "teacher":
        _validate_teacher_sections(body.section_ids)
    uid = database.create_user(body.username, body.full_name, security.hash_password(body.password), body.role)
    if body.role == "teacher":
        database.set_teacher_sections(uid, body.section_ids)
    audit(actor, "user_created", f"{body.username} ({body.role})")
    return _public_row(database.get_user(uid))


@app.patch("/api/users/{uid}")
def users_update(uid: int, body: UserPatch, actor=Depends(require("users.manage"))):
    target = _manageable_target(actor, uid)
    new_role = body.role or target["role"]
    if not security.can_manage(actor["role"], new_role):
        raise HTTPException(403, f"You cannot assign the {security.ROLE_LABEL[new_role]} role")
    if uid == actor["id"] and (new_role != target["role"] or body.active is False):
        raise HTTPException(400, "You cannot change your own account role or status")
    if new_role == "teacher" and body.section_ids is not None:
        _validate_teacher_sections(body.section_ids)
    fields = {"role": new_role}
    if body.full_name is not None:
        fields["full_name"] = body.full_name.strip()
    if body.active is not None:
        fields["active"] = int(body.active)
    database.update_user(uid, **fields)
    if new_role == "teacher" and body.section_ids is not None:
        database.set_teacher_sections(uid, body.section_ids)
    elif new_role != "teacher":
        database.set_teacher_sections(uid, [])
    if new_role != target["role"] or body.active is False:
        database.delete_user_sessions(uid)        # take effect immediately
    audit(actor, "user_updated", f"{target['username']}: {fields}")
    return _public_row(database.get_user(uid))


@app.post("/api/users/{uid}/reset-password")
def users_reset_password(uid: int, body: ResetPasswordIn, actor=Depends(require("users.manage"))):
    target = _manageable_target(actor, uid)
    database.update_user(uid, password_hash=security.hash_password(body.password))
    database.delete_user_sessions(uid)
    audit(actor, "password_reset", target["username"])
    return {"ok": True}


@app.delete("/api/users/{uid}")
def users_delete(uid: int, actor=Depends(require("users.manage"))):
    target = _manageable_target(actor, uid)
    if uid == actor["id"]:
        raise HTTPException(400, "You cannot delete your own account")
    database.delete_user(uid)
    audit(actor, "user_deleted", target["username"])
    return {"ok": True}


# =============================================================== face engine glue
_engine_lock = threading.RLock()
_matcher = None
_streaks = {}     # session -> (last_seen_ts, {student_id: consecutive scans})


def get_matcher():
    global _matcher
    with _engine_lock:
        if _matcher is None:
            try:
                face_engine._fr()
            except Exception as e:     # dlib / face_recognition not installed, or pkg_resources problem
                raise HTTPException(503, f"The face_recognition module could not be loaded: {e}")
            _matcher = face_engine.FaceMatcher()
        return _matcher


def reload_matcher():
    with _engine_lock:
        if _matcher is not None:
            _matcher.reload()


def decode_image(data_url):
    try:
        raw = base64.b64decode(data_url.split(",", 1)[-1], validate=False)
        return face_engine.load_image(io.BytesIO(raw))
    except (binascii.Error, ValueError, OSError):
        raise HTTPException(400, "The image could not be read. Please use a JPG or PNG file.")


def avatar_path(sid):
    return config.AVATAR_DIR / f"{sid}.jpg"


def save_avatar(sid, rgb, loc):
    from PIL import Image
    top, right, bottom, left = loc
    m = int(0.4 * max(bottom - top, right - left))
    img = Image.fromarray(rgb).crop((max(left - m, 0), max(top - m, 0), right + m, bottom + m))
    img.thumbnail((220, 220))
    config.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    img.save(avatar_path(sid), quality=85)


def enroll_images(sid, images):
    """Encode each photo for student `sid`. Returns (per-photo results, possible-duplicate name or None)."""
    results, duplicate, had_avatar = [], None, avatar_path(sid).exists()
    with _engine_lock:
        matcher = get_matcher()
        matcher.threshold = database.get_settings()["threshold"]
        for i, data in enumerate(images, 1):
            try:
                rgb = decode_image(data)
                loc, enc = face_engine.enroll_face(rgb)
            except HTTPException as e:
                results.append({"photo": i, "ok": False, "error": e.detail})
                continue
            except ValueError as e:
                results.append({"photo": i, "ok": False, "error": str(e)})
                continue
            other, _, other_name = matcher.match(enc)
            if other and other != sid:
                duplicate = other_name
            database.add_encoding(sid, enc)
            if not had_avatar:
                save_avatar(sid, rgb, loc)
                had_avatar = True
            results.append({"photo": i, "ok": True})
    reload_matcher()
    return results, duplicate


# =============================================================== students
def _student_row(s):
    return {**s, "has_avatar": avatar_path(s["id"]).exists()}


@app.get("/api/students")
def students_list(q: str = "", class_name: str = "", section_id: int = 0, user=Depends(current_user)):
    allowed = allowed_sections(user)

    if allowed is not None:
        if not allowed:
            return {"students": [], "classes": [], "sections": []}
        if section_id:
            if section_id == -1 or section_id not in allowed:
                raise HTTPException(403, "This section is not assigned to you.")
        elif len(allowed) == 1:
            section_id = next(iter(allowed))
        if class_name:
            own_names = {s["name"] for s in database.list_sections(allowed)}
            if class_name not in own_names:
                raise HTTPException(403, "This section is not assigned to you.")

    rows = database.list_students(q.strip() or None, class_name or None, section_id or None, allowed)
    return {"students": [_student_row(s) for s in rows],
            "classes": [x["name"] for x in database.list_sections(allowed)],
            "sections": database.list_sections(allowed)}


@app.get("/api/students/{sid}/avatar")
def student_avatar(sid: int, user=Depends(current_user)):
    student = database.get_student(sid)
    if not student:
        raise HTTPException(404, "Student not found")
    allowed = allowed_sections(user)
    if allowed is not None and student["section_id"] not in allowed:
        raise HTTPException(403, "This student is outside a section assigned to you.")
    p = avatar_path(sid)
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "private, max-age=3600"})


def _check_section(section_id):
    if section_id and not database.get_section(section_id):
        raise HTTPException(404, "Section not found")


@app.post("/api/students/register", status_code=201)
def students_register(body: RegisterIn, user=Depends(require("students.manage"))):
    if database.get_student_by_name(body.name):
        raise HTTPException(409, "A student with this name already exists. Add photos from the student's profile.")
    _check_section(body.section_id)
    try:
        sid = database.add_student(body.name, body.roll_no, body.section_id)
    except ValueError as e:
        raise HTTPException(409, str(e))
    results, duplicate = enroll_images(sid, body.images)
    added = sum(r["ok"] for r in results)
    if added == 0:
        database.delete_student(sid)       # nothing usable -> don't leave an empty student behind
        raise HTTPException(422, "No photo could be used: " + "; ".join(
            f"photo {r['photo']}: {r['error']}" for r in results))
    audit(user, "student_registered", f"{body.name} ({added} photos)")
    return {"student": _student_row(database.get_student(sid)), "results": results, "added": added,
            "possible_duplicate": duplicate}


@app.post("/api/students/{sid}/photos")
def students_add_photos(sid: int, body: PhotosIn, user=Depends(require("students.manage"))):
    student = database.get_student(sid)
    if not student:
        raise HTTPException(404, "Student not found")
    results, duplicate = enroll_images(sid, body.images)
    added = sum(r["ok"] for r in results)
    audit(user, "photos_added", f"{student['name']} (+{added})")
    return {"results": results, "added": added, "possible_duplicate": duplicate}


@app.delete("/api/students/{sid}/photos")
def students_reset_faces(sid: int, user=Depends(require("students.manage"))):
    student = database.get_student(sid)
    if not student:
        raise HTTPException(404, "Student not found")
    database.delete_encodings(sid)
    avatar_path(sid).unlink(missing_ok=True)
    reload_matcher()
    audit(user, "face_data_reset", student["name"])
    return {"ok": True}


@app.post("/api/students/assign-section")
def students_assign_section(body: AssignSectionIn, user=Depends(require("students.manage"))):
    _check_section(body.section_id)
    n = database.assign_section(body.student_ids, body.section_id)
    sec = database.get_section(body.section_id) if body.section_id else None
    audit(user, "students_moved", f"{n} -> {sec['name'] if sec else 'unassigned'}")
    return {"moved": n}


@app.patch("/api/students/{sid}")
def students_update(sid: int, body: StudentIn, user=Depends(require("students.manage"))):
    if not database.get_student(sid):
        raise HTTPException(404, "Student not found")
    clash = database.get_student_by_name(body.name)
    if clash and clash["id"] != sid:
        raise HTTPException(409, "Another student with this name already exists")
    _check_section(body.section_id)
    try:
        database.update_student(sid, body.name, body.roll_no, body.section_id)
    except ValueError as e:
        raise HTTPException(409, str(e))
    reload_matcher()                      # names are cached in the matcher
    audit(user, "student_updated", body.name)
    return _student_row(database.get_student(sid))


@app.delete("/api/students/{sid}")
def students_delete(sid: int, user=Depends(require("students.manage"))):
    student = database.get_student(sid)
    if not student:
        raise HTTPException(404, "Student not found")
    database.delete_student(sid)
    avatar_path(sid).unlink(missing_ok=True)
    reload_matcher()
    audit(user, "student_deleted", student["name"])
    return {"ok": True}


# =============================================================== attendance
def _mark(student, distance, day=None, at=None, teacher_id=None, section_id=None):
    """-> (state, status, window). state: marked | already | closed (outside the section's class time)."""
    selected = None
    if day:
        selected = datetime.combine(day, at or datetime.now().time().replace(microsecond=0))
    status, window = database.status_for_student(student, selected)
    if status == "closed":
        return "closed", None, window
    selected_time = at.strftime("%H:%M:%S") if at else None
    new, status = database.mark_attendance_ex(student["id"], distance, status,
                                               day.isoformat() if day else None, selected_time,
                                               section_id, teacher_id)
    return ("marked" if new else "already"), status, window


def _scan_section(body, user):
    """Which section is this scan for? Teachers must pick one of their own; admins may leave it empty (= all)."""
    allowed = allowed_sections(user)
    section_id = body.section_id
    if allowed is not None:
        if not allowed:
            raise HTTPException(403, "No section has been assigned to you. Ask an admin to assign one on the Sections page.")
        if not section_id and len(allowed) == 1:
            section_id = next(iter(allowed))
        if not section_id:
            raise HTTPException(400, "First select the section for which you want to take attendance.")
        if section_id not in allowed:
            raise HTTPException(403, "This section is not assigned to you.")
    if section_id and not database.get_section(section_id):
        raise HTTPException(404, "Section not found")
    return section_id


@app.post("/api/attendance/scan")
def attendance_scan(body: ScanIn, user=Depends(require("attendance.take"))):
    """One camera frame (live) or one uploaded photo. Recognises every face and, if `mark`, saves attendance."""
    section_id = _scan_section(body, user)
    rgb = decode_image(body.image)
    settings = database.get_settings()
    with _engine_lock:
        matcher = get_matcher()
        matcher.threshold = settings["threshold"]
        results = face_engine.recognize_rgb(rgb, matcher)
        registered = matcher.size

    key, now = user["session_key"], time.time()
    last_ts, last_streak = _streaks.get(key, (0, {}))
    last_streak = last_streak if now - last_ts < 5 else {}
    streak, seen, faces, newly = {}, set(), [], []
    need = settings["confirm_frames"]

    for r in results:
        sid = r["student_id"]
        face = {"box": list(r["location"]), "student_id": sid, "name": r["name"],
                "distance": None if r["distance"] is None else round(r["distance"], 3), "state": "unknown"}
        student = database.get_student(sid) if sid else None
        if sid and not student:
            face.update(student_id=None, name="Unknown")
            sid = None
        if sid and section_id and student["section_id"] != section_id:
            face.update(state="other_section", name="Student from another section")   # not shown to this teacher
        elif sid and sid in seen:
            face["state"] = "recognised"                        # same person twice in one frame
        elif sid:
            seen.add(sid)
            face["state"] = "recognised"
            if body.mark and body.mode == "photo":
                face["state"], face["status"], face["window"] = _mark(
                    student, r["distance"], body.day, body.at, user["id"], section_id)
            elif body.mark:
                n = streak[sid] = last_streak.get(sid, 0) + 1   # live: must be seen N scans in a row
                if n >= need:
                    face["state"], face["status"], face["window"] = _mark(
                        student, r["distance"], body.day, body.at, user["id"], section_id)
                else:
                    face["state"], face["progress"] = "confirming", [n, need]
            if face["state"] == "marked":
                newly.append(r["name"])
        faces.append(face)

    _streaks[key] = (now, streak)
    if len(_streaks) > 100:
        for k in [k for k, (t, _) in _streaks.items() if now - t > 60]:
            del _streaks[k]
    if newly:
        audit(user, "attendance_scan", ", ".join(newly))
    h, w = rgb.shape[:2]
    return {"image_size": {"w": w, "h": h}, "faces": faces, "registered": registered, "marked": newly}


def _parse_date(value, field):
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise HTTPException(422, f"{field}: invalid date (YYYY-MM-DD)")


def _filtered_attendance(user, start, end, student_id, class_name, status):
    return database.get_attendance(_parse_date(start, "start"), _parse_date(end, "end"),
                                   student_id or None, class_name or None, status or None,
                                   section_ids=allowed_sections(user))


@app.get("/api/attendance")
def attendance_list(start: str = "", end: str = "", student_id: int = 0, class_name: str = "", status: str = "",
                    user=Depends(current_user)):
    return {"records": _filtered_attendance(user, start, end, student_id, class_name, status)}


def _csv_safe(v):
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@") else v       # block spreadsheet formula injection


@app.get("/api/attendance/export.csv")
def attendance_export(start: str = "", end: str = "", student_id: int = 0, class_name: str = "", status: str = "",
                      user=Depends(current_user)):
    rows = _filtered_attendance(user, start, end, student_id, class_name, status)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Name", "Roll no", "Class", "Date", "Time", "Status"])
    for r in rows:
        w.writerow([_csv_safe(r["name"]), _csv_safe(r["roll_no"]), _csv_safe(r["class_name"]),
                    r["date"], r["time"], r["status"]])
    audit(user, "csv_export", f"{len(rows)} rows")
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="attendance_{date.today()}.csv"'})


@app.post("/api/attendance/manual", status_code=201)
def attendance_manual(body: ManualIn, user=Depends(require("attendance.edit"))):
    student = database.get_student(body.student_id)
    if not student:
        raise HTTPException(404, "Student not found")
    allowed = allowed_sections(user)
    if allowed is not None and student["section_id"] not in allowed:
        raise HTTPException(403, "This student is not in a section assigned to you.")
    day = (body.day or date.today()).isoformat()
    at = body.at.strftime("%H:%M:%S") if body.at else None
    if not database.mark_attendance_ex(body.student_id, None, body.status, day, at,
                                       student["section_id"], user["id"])[0]:
        raise HTTPException(409, "Attendance is already marked for that date")
    audit(user, "attendance_manual", f"{student['name']} {day} {body.status}")
    return {"ok": True}


@app.delete("/api/attendance/{aid}")
def attendance_delete(aid: int, user=Depends(require("attendance.edit"))):
    record = database.get_attendance_record(aid)
    if not record:
        raise HTTPException(404, "Record not found")
    allowed = allowed_sections(user)
    if allowed is not None:
        if record["section_id"] not in allowed:
            raise HTTPException(403, "This record is outside a section assigned to you.")
        if record["teacher_id"] != user["id"]:
            raise HTTPException(403, "This record belongs to another teacher.")
    if not database.delete_attendance(aid):
        raise HTTPException(404, "Record not found")
    audit(user, "attendance_deleted", str(aid))
    return {"ok": True}


@app.get("/api/dashboard")
def dashboard(user=Depends(current_user)):
    today = date.today().isoformat()
    allowed = allowed_sections(user)
    students = database.list_students(section_ids=allowed)
    present = database.get_attendance(today, today, section_ids=allowed)
    present_ids = {r["student_id"] for r in present}
    late_count = sum(r["status"] == "late" for r in present)
    absent = [{"name": s["name"], "roll_no": s["roll_no"], "class_name": s["class_name"]}
              for s in students if s["id"] not in present_ids]
    counts = {r["date"]: r["present"] for r in database.daily_counts(14)} if allowed is None else {}
    if allowed is not None:
        for r in database.get_attendance((date.today() - timedelta(days=13)).isoformat(), today,
                                         section_ids=allowed):
            counts[r["date"]] = counts.get(r["date"], 0) + 1
    series = [{"date": (date.today() - timedelta(days=i)).isoformat(),
               "present": counts.get((date.today() - timedelta(days=i)).isoformat(), 0)} for i in range(13, -1, -1)]

    groups = [(sec["id"], sec["name"], sec) for sec in database.list_sections(allowed)]
    if allowed is None and any(s["section_id"] is None for s in students):
        groups.append((None, "Unassigned", None))
    by_section = []
    for sid, name, sec in groups:
        ids = {s["id"] for s in students if s["section_id"] == sid}
        by_section.append({"name": name, "total": len(ids), "present": len(ids & present_ids),
                           "absent": len(ids - present_ids),
                           "rate": round(100 * len(ids & present_ids) / len(ids)) if ids else 0,
                           "class_start": sec and sec["class_start"], "class_end": sec and sec["class_end"]})
    return {
        "total": len(students), "present": len(present_ids),
        "late": late_count,
        "absent": len(absent),
        "rate": round(100 * len(present_ids) / len(students)) if students else 0,
        "present_list": present, "absent_list": absent, "series": series, "sections": by_section,
        "no_sections": allowed is not None and not allowed,
    }


# =============================================================== sections
def _valid_teacher_ids(ids):
    users = {u["id"]: u for u in database.list_users()}
    bad = [i for i in ids if i not in users or users[i]["role"] != "teacher"]
    if bad:
        raise HTTPException(422, "Only users with the Teacher role can be assigned")


@app.get("/api/sections")
def sections_list(user=Depends(current_user)):
    out = {"sections": database.list_sections(allowed_sections(user))}
    if user["role"] in security.PERMS["sections.manage"]:
        out["teachers"] = [{"id": u["id"], "full_name": u["full_name"], "username": u["username"]}
                           for u in database.list_users() if u["role"] == "teacher" and u["active"]]
    return out


@app.post("/api/sections", status_code=201)
def sections_create(body: SectionIn, user=Depends(require("sections.manage"))):
    _valid_teacher_ids(body.teacher_ids)
    try:
        sid = database.create_section(body.name, body.class_start, body.class_end, body.late_after_min, body.teacher_ids)
    except ValueError as e:
        raise HTTPException(409, str(e))
    audit(user, "section_created", body.name)
    return next(x for x in database.list_sections() if x["id"] == sid)


@app.put("/api/sections/{sid}")
def sections_update(sid: int, body: SectionIn, user=Depends(require("sections.manage"))):
    _check_section(sid)
    _valid_teacher_ids(body.teacher_ids)
    try:
        database.update_section(sid, body.name, body.class_start, body.class_end, body.late_after_min, body.teacher_ids)
    except ValueError as e:
        raise HTTPException(409, str(e))
    audit(user, "section_updated", f"{body.name} {body.class_start}-{body.class_end} grace {body.late_after_min}")
    return next(x for x in database.list_sections() if x["id"] == sid)


@app.delete("/api/sections/{sid}")
def sections_delete(sid: int, user=Depends(require("sections.manage"))):
    sec = database.get_section(sid)
    if not sec:
        raise HTTPException(404, "Section not found")
    database.delete_section(sid)
    audit(user, "section_deleted", sec["name"])
    return {"ok": True}


# =============================================================== settings + audit
@app.get("/api/settings")
def settings_get(user=Depends(current_user)):
    return database.get_settings()


@app.put("/api/settings")
def settings_put(body: SettingsIn, user=Depends(require("settings.edit"))):
    database.set_settings(body.model_dump())
    audit(user, "settings_changed", str(body.model_dump()))
    return database.get_settings()


@app.get("/api/audit")
def audit_list(limit: int = 100, user=Depends(require("audit.view"))):
    return {"entries": database.list_audit(min(max(limit, 1), 500))}


# =============================================================== pages
def _page(name):
    def handler():
        return FileResponse(STATIC / name)
    return handler


@app.get("/", include_in_schema=False)
def home(request: Request):
    """Logged-in users land on the dashboard exactly as before; visitors who are not logged in see the landing page."""
    token = request.cookies.get(COOKIE)
    user = database.get_session_user(token) if token else None
    return FileResponse(STATIC / ("index.html" if user and user["active"] else "landing.html"))


for _path, _file in {"/login": "login.html", "/attendance": "attendance.html",
                     "/students": "students.html", "/sections": "sections.html", "/register": "register.html", "/records": "records.html",
                     "/users": "users.html", "/settings": "settings.html"}.items():
    app.add_api_route(_path, _page(_file), include_in_schema=False)

app.mount("/static", StaticFiles(directory=STATIC), name="static")
