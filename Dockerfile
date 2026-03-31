# Use a slim Python base
FROM python:3.11-slim

# Set a non-root user
ENV PYTHONUNBUFFERED=1
RUN addgroup --system exporter && adduser --system --ingroup exporter exporter

WORKDIR /app

# Copy files
COPY exporter.py /app/exporter.py
COPY requirements.txt /app/requirements.txt

# Install dependencies
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev \
    && pip install --no-cache-dir -r /app/requirements.txt \
    && apt-get remove -y gcc libpq-dev \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Set permissions
RUN chown -R exporter:exporter /app
USER exporter

EXPOSE 9410

ENTRYPOINT ["python", "/app/exporter.py"]