"""Check required runtime dependencies before starting Face Attendance."""
import importlib
import sys

MODULES = {
    "fastapi": "FastAPI backend",
    "uvicorn": "ASGI web server",
    "numpy": "Face encoding arrays",
    "PIL": "Image processing (Pillow)",
    "face_recognition": "Face recognition engine",
    "dlib": "Face detection/encoding runtime",
}

failed = []
print(f"Python: {sys.version.split()[0]} ({sys.executable})\n")
for module, purpose in MODULES.items():
    try:
        imported = importlib.import_module(module)
        version = getattr(imported, "__version__", "available")
        print(f"[OK]   {module}: {version} - {purpose}")
    except Exception as exc:
        failed.append(module)
        print(f"[FAIL] {module}: {type(exc).__name__}: {exc} - {purpose}")

print("\n" + ("All required checks passed. Start with: uvicorn api:app --host 127.0.0.1 --port 8000" if not failed else "Missing/broken modules: " + ", ".join(failed) + ". Install requirements and resolve dlib/face_recognition before using camera recognition."))
sys.exit(1 if failed else 0)
