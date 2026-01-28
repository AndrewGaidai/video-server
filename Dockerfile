FROM python:3.11-slim

WORKDIR /app

# Install ffmpeg and other dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Expose port
EXPOSE 8080

# Run the application
CMD ["python", "video_server.py"]

RUN apt-get update && apt-get install -y imagemagick