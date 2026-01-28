FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    imagemagick \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Fix ImageMagick security policy
RUN sed -i 's/<policy domain="path" rights="none" pattern="@\*"/<policy domain="path" rights="read|write" pattern="@\*"/' /etc/ImageMagick-6/policy.xml

# Copy application files
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY video_server.py .

# Create necessary directories
RUN mkdir -p temp_images output_videos

EXPOSE 8080

CMD ["python", "video_server.py"]