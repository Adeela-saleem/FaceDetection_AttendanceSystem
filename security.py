"""Passwords and roles."""
import hashlib
import hmac
import os

ROLES = ("admin", "teacher")
ROLE_LABEL = {"admin": "Admin", "teacher": "Teacher"}

# permission -> roles that have it. Everyone logged in can *view* dashboard / students / records.
PERMS = {
    "attendance.take": {"admin", "teacher"},
    "attendance.edit": {"admin", "teacher"},
    "students.manage": {"admin"},
    "sections.manage": {"admin"},
    "users.manage": {"admin"},
    "settings.edit": {"admin"},
    "audit.view": {"admin"},
}

# which roles an actor is allowed to create / edit / delete
MANAGEABLE = {"admin": {"teacher"}}


def can_manage(actor_role, target_role):
    return target_role in MANAGEABLE.get(actor_role, set())


def hash_password(password):
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_password(password, stored):
    try:
        _, salt, h = stored.split("$")
        calc = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


DUMMY_HASH = hash_password("not-a-real-password")   # verified when username is unknown (same timing)
