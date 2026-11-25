FROM python:3.11-slim

# dlib needs a C++ toolchain + cmake to build
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# headless OpenCV on servers
RUN sed -i 's/^opencv-python /opencv-python-headless /' requirements.txt \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

# keep the database and face thumbnails on a volume
ENV ATTENDANCE_DB=/data/attendance.db \
    ATTENDANCE_AVATARS=/data/avatars
VOLUME /data

EXPOSE 8000
# Put this behind HTTPS (reverse proxy) and add  -e ATTENDANCE_COOKIE_SECURE=1  -- browsers only allow the camera on HTTPS/localhost.
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
