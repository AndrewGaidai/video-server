FROM python:3.11-slim

# MoviePy needs ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

# Flask/Gunicorn port
ENV PORT=8080

# Start with gunicorn (production-safe)
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:8080", "video_server:app", "--timeout", "300"]
