"""Import photos from the Known_Faces folder into the database.

Works with both layouts:
  Known_Faces/Adeela1.jpg, Adeela2.jpg, Ali.jpg      (name = filename without digits/extension)
  Known_Faces/Adeela/front.jpg, left.jpg             (name = folder name)

Run:  python import_known_faces.py                 (students without a section)
      python import_known_faces.py "BSCS-6A"        (put them all in that section; created if missing)
"""
import re
import sys
from pathlib import Path

import config
import database
from face_engine import encode_enrollment_image, load_image

EXTS = {".jpg", ".jpeg", ".png"}


def student_name_from(path: Path) -> str:
    if path.parent != config.PHOTOS_DIR:
        return path.parent.name.replace("_", " ").strip()
    return re.sub(r"[\d_\-\s]+$", "", path.stem).strip() or path.stem


def main():
    database.init_db()
    files = sorted(p for p in config.PHOTOS_DIR.rglob("*") if p.suffix.lower() in EXTS)
    if not files:
        print(f"No photos were found in {config.PHOTOS_DIR}.")
        return

    section_id = None
    if len(sys.argv) > 1:
        sec = database.get_section_by_name(sys.argv[1])
        section_id = sec["id"] if sec else database.create_section(sys.argv[1])
        print(f"Section: {sys.argv[1]}")

    ok = bad = 0
    for path in files:
        name = student_name_from(path)
        try:
            enc = encode_enrollment_image(load_image(path))
        except Exception as e:
            print(f"  SKIP {path.name}: {e}")
            bad += 1
            continue
        student = database.get_student_by_name(name)
        sid = student["id"] if student else database.add_student(name, section_id=section_id)
        database.add_encoding(sid, enc)
        print(f"  OK   {path.name} -> {name}")
        ok += 1
    print(f"\nDone: {ok} photos imported, {bad} skipped.")
    if not section_id:
        print("Students are not assigned to a section. On the Students page, select them and use 'Move to section' (roll numbers will be assigned automatically).")
    print("Note: Running this again will add the same photos again. Delete attendance.db first if you want a fresh import.")


if __name__ == "__main__":
    main()
