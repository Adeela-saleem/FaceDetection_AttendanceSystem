"""Live camera attendance. Run:  python recognize_live.py

Keys:  q = quit    r = reload registered students (after adding someone in the web app)

Note: a webcam can only be used by one program at a time. Close this script before using the
browser camera in the web app (and vice versa), otherwise the browser says "camera busy".
"""
import sys
import time

import cv2

import config
import database
from face_engine import FaceMatcher, recognize_rgb

GREEN, RED, WHITE = (0, 200, 0), (0, 0, 255), (255, 255, 255)


def draw_label(frame, text, left, top, color):
    cv2.rectangle(frame, (left, top - 28), (left + 12 * len(text) + 10, top), color, -1)
    cv2.putText(frame, text, (left + 5, top - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.6, WHITE, 2)


def main():
    database.init_db()
    matcher = FaceMatcher()
    if matcher.size == 0:
        print("No registered student found. Register a student in the web app (uvicorn api:app),")
        print("or import existing photos: python import_known_faces.py")
        return 1
    print(f"Loaded {len(set(matcher.ids))} students ({matcher.size} photos). q = quit, r = reload")

    cap = cv2.VideoCapture(config.CAMERA_INDEX)
    if not cap.isOpened():
        print("The camera could not be opened. Check CAMERA_INDEX in config.py or close any app currently using the camera.")
        return 1

    confirm = database.get_settings()["confirm_frames"]
    streak = {}          # student_id -> consecutive processed frames recognised
    banners = {}         # student_id -> (text, show_until)
    last_results = []    # drawn on every frame so labels don't flicker between processed frames
    frame_no = 0
    inv = 1.0 / config.FRAME_SCALE

    while True:
        ok, frame = cap.read()
        if not ok:
            print("No camera frame was received. Closing.")
            break
        frame_no += 1

        if frame_no % config.PROCESS_EVERY_N_FRAMES == 0:
            small = cv2.resize(frame, (0, 0), fx=config.FRAME_SCALE, fy=config.FRAME_SCALE)
            results = recognize_rgb(cv2.cvtColor(small, cv2.COLOR_BGR2RGB), matcher)
            for r in results:
                r["location"] = tuple(int(v * inv) for v in r["location"])

            seen = set()
            for r in results:
                sid = r["student_id"]
                if sid is None:
                    continue
                seen.add(sid)
                streak[sid] = streak.get(sid, 0) + 1
                # Only act once the same person is seen N frames in a row -> no false marks from one bad frame
                if streak[sid] == confirm:
                    if database.mark_attendance(sid, r["distance"]):
                        print(f"[{time.strftime('%H:%M:%S')}] ATTENDANCE MARKED: {r['name']} (distance {r['distance']:.2f})")
                        banners[sid] = (f"{r['name']} - ATTENDANCE MARKED", time.time() + 3)
                    else:
                        banners[sid] = (f"{r['name']} - already marked today", time.time() + 2)
            for sid in list(streak):
                if sid not in seen:
                    del streak[sid]
            last_results = results

        for r in last_results:
            top, right, bottom, left = r["location"]
            color = GREEN if r["student_id"] else RED
            cv2.rectangle(frame, (left, top), (right, bottom), color, 2)
            label = r["name"] if r["student_id"] is None else f"{r['name']} ({r['distance']:.2f})"
            draw_label(frame, label, left, top, color)

        y = 30
        for sid, (text, until) in list(banners.items()):
            if time.time() > until:
                del banners[sid]
                continue
            cv2.rectangle(frame, (0, y - 24), (12 * len(text) + 20, y + 8), (0, 120, 0), -1)
            cv2.putText(frame, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.7, WHITE, 2)
            y += 40

        cv2.imshow("Face Attendance  (q = quit, r = reload)", frame)
        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            break
        if key == ord("r"):
            matcher.reload()
            print(f"Reloaded: {len(set(matcher.ids))} students")

    cap.release()
    cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    sys.exit(main())
