"""Test setup: temp database + a fake `face_recognition` module so tests run without dlib."""
import os
import sys
import tempfile
import types
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_tmp = tempfile.mkdtemp()
os.environ["ATTENDANCE_DB"] = os.path.join(_tmp, "test.db")
os.environ["ATTENDANCE_PHOTOS"] = os.path.join(_tmp, "photos")


def _install_fake_face_recognition():
    fake = types.ModuleType("face_recognition")

    def face_locations(img, number_of_times_to_upsample=1, model="hog"):
        return [(10, 60, 60, 10)]          # one fake face per image

    def face_encodings(img, locations=None, **kw):
        # deterministic "encoding" from the image's mean brightness
        rng = np.random.default_rng(int(np.asarray(img).mean()))
        return [rng.normal(size=128) * 0.1 for _ in (locations or [])]

    fake.face_locations = face_locations
    fake.face_encodings = face_encodings
    sys.modules["face_recognition"] = fake


_install_fake_face_recognition()
