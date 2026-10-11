# Use official Python lightweight image
FROM python:3.11-slim

# Prevent Python from writing .pyc files and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project source code
COPY . .

# Create required upload, output, and log directories
RUN mkdir -p uploads outputs logs

# Expose web port
EXPOSE 8080

# Environment variables
ENV PORT=8080
ENV UPLOAD_FOLDER=uploads
ENV OUTPUT_FOLDER=outputs
ENV LOG_FOLDER=logs

# Run application using gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "server.app:app", "--timeout", "600", "--workers", "2"]
