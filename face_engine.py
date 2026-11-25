"""Face detection, encoding and matching (the 'engine').

Pipeline:  image -> detect faces -> 128-d encoding per face -> compare with registered
encodings -> best student below THRESHOLD (else Unknown).
"""
import numpy as np
from PIL import Image, ImageOps

import config
import database


def _fr():
    # Imported lazily so the rest of the project (database, tests) works without dlib installed.
    import face_recognition
    return face_recognition


# ---------------- image helpers ----------------
def load_image(file_or_path):
    """Open an image (path or uploaded file), fix phone rotation, shrink, return RGB numpy array."""
    img = ImageOps.exif_transpose(Image.open(file_or_path)).convert("RGB")
    longest = max(img.size)
    if longest > config.MAX_IMAGE_SIDE:
        s = config.MAX_IMAGE_SIDE / longest
        img = img.resize((int(img.width * s), int(img.height * s)))
    return np.array(img)


def find_faces(rgb):
    """Returns (locations[(top,right,bottom,left)], encodings) for every face in an RGB image."""
    fr = _fr()
    rgb = np.ascontiguousarray(rgb)
    locations = fr.face_locations(rgb, model=config.DETECTION_MODEL)
    encodings = fr.face_encodings(rgb, locations)
    return locations, encodings


def enroll_face(rgb):
    """For registering a student: the photo must contain exactly one face. Returns (location, encoding)."""
    locations, encodings = find_faces(rgb)
    if len(encodings) == 0:
        raise ValueError("No face was detected in this photo")
    if len(encodings) > 1:
        raise ValueError("More than one face was detected in this photo")
    return locations[0], encodings[0]


def encode_enrollment_image(rgb):
    return enroll_face(rgb)[1]


# ---------------- matching ----------------
def best_match(encoding, known_encodings, known_ids, threshold):
    """Pure-numpy matcher. Returns (student_id or None, best_distance or None).

    A student can have several photos; their score is the closest of their photos.
    """
    if len(known_ids) == 0:
        return None, None
    distances = np.linalg.norm(known_encodings - np.asarray(encoding), axis=1)
    per_student = {}
    for sid, d in zip(known_ids, distances):
        if sid not in per_student or d < per_student[sid]:
            per_student[sid] = float(d)
    sid = min(per_student, key=per_student.get)
    dist = per_student[sid]
    return (sid if dist < threshold else None), dist


class FaceMatcher:
    """Holds all registered encodings in memory (loaded from the database)."""

    def __init__(self, threshold=None):
        self.threshold = database.get_settings()["threshold"] if threshold is None else threshold
        self.reload()

    def reload(self):
        self.encodings, self.ids, self.names = database.load_all_encodings()

    @property
    def size(self):
        return len(self.ids)

    def match(self, encoding):
        sid, dist = best_match(encoding, self.encodings, self.ids, self.threshold)
        return sid, (self.names.get(sid, "Unknown") if sid else "Unknown"), dist


def recognize_rgb(rgb, matcher):
    """Detect + identify every face in an RGB image.

    Returns a list of dicts: location (top,right,bottom,left), student_id, name, distance.
    """
    locations, encodings = find_faces(rgb)
    results = []
    for loc, enc in zip(locations, encodings):
        sid, name, dist = matcher.match(enc)
        results.append({"location": loc, "student_id": sid, "name": name, "distance": dist})
    return results
