FROM python:3.11-slim

# Install runtime and build dependencies (including unar and 7zip for archive extraction)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    python3-dev \
    unar \
    p7zip-full \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY app.py .
COPY static/ ./static/
COPY config.example.json .

# Create persistent directories
RUN mkdir -p /app/data /app/downloads

# Default environment variables
ENV HOST=0.0.0.0 \
    PORT=8765 \
    DOWNLOAD_DIR=/app/downloads \
    SESSION_PATH=/app/data/tg \
    PYTHONUNBUFFERED=1

EXPOSE 8765

CMD ["python", "app.py"]
