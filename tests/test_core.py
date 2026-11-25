import numpy as np
import pytest

import database
import face_engine


@pytest.fixture(autouse=True)
def fresh_db():
    import config
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    database.init_db()


def test_student_and_encoding_roundtrip():
    sid = database.add_student("Adeela", "CS-01")
    enc = np.random.default_rng(1).normal(size=128)
    database.add_encoding(sid, enc)
    encs, ids, names = database.load_all_encodings()
    assert encs.shape == (1, 128) and ids == [sid] and names[sid] == "Adeela"
    assert np.allclose(encs[0], enc)


def test_duplicate_name_rejected_case_insensitive_lookup():
    database.add_student("Ali")
    with pytest.raises(Exception):
        database.add_student("Ali")
    assert database.get_student_by_name("ali") is not None


def test_attendance_once_per_day():
    sid = database.add_student("Sara")
    assert database.mark_attendance(sid, 0.31) is True
    assert database.mark_attendance(sid, 0.30) is False
    rows = database.get_attendance()
    assert len(rows) == 1 and rows[0]["name"] == "Sara"
    assert len(database.daily_counts(14)) == 1


def test_delete_student_cascades():
    sid = database.add_student("Temp")
    database.add_encoding(sid, np.zeros(128))
    database.mark_attendance(sid)
    database.delete_student(sid)
    assert database.list_students() == []
    assert database.get_attendance() == []
    assert database.load_all_encodings()[1] == []


def test_best_match_multi_photo_and_threshold():
    rng = np.random.default_rng(0)
    a, b = rng.normal(size=128), rng.normal(size=128)
    encs = np.vstack([a, a + 0.01, b])           # student 1 has two photos, student 2 one
    ids = [1, 1, 2]

    sid, dist = face_engine.best_match(a + 0.005, encs, ids, threshold=0.5)
    assert sid == 1 and dist < 0.1

    stranger = rng.normal(size=128)
    sid, dist = face_engine.best_match(stranger, encs, ids, threshold=0.5)
    assert sid is None and dist > 0.5            # unknown person is rejected

    assert face_engine.best_match(a, np.empty((0, 128)), [], 0.5) == (None, None)


def test_matcher_and_recognize_with_fake_detector():
    sid = database.add_student("Hamza")
    img = np.full((100, 100, 3), 120, dtype=np.uint8)
    _, enc = face_engine.find_faces(img)
    database.add_encoding(sid, enc[0])

    matcher = face_engine.FaceMatcher()
    res = face_engine.recognize_rgb(img, matcher)
    assert res[0]["name"] == "Hamza" and res[0]["distance"] < 1e-6

    other = np.full((100, 100, 3), 200, dtype=np.uint8)   # different "person"
    assert face_engine.recognize_rgb(other, matcher)[0]["name"] == "Unknown"
