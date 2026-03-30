# Use a small Python base
FROM python:3.11-slim

# Create non-root user
ENV APP_USER=exporter
RUN groupadd -r $APP_USER && useradd -r -g $APP_USER $APP_USER

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy app
COPY exporter.py /app/exporter.py
RUN chown -R $APP_USER:$APP_USER /app

USER $APP_USER

# Expose metrics port
EXPOSE 8000

ENV HTTP_PORT=8000
ENV PYTHONUNBUFFERED=1

CMD ["python", "/app/exporter.py"]
