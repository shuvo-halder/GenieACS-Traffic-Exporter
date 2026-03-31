FROM python:3.11-slim

# Build args for proxy (optional)
ARG HTTP_PROXY
ARG HTTPS_PROXY
ENV HTTP_PROXY=${HTTP_PROXY}
ENV HTTPS_PROXY=${HTTPS_PROXY}
ENV PYTHONUNBUFFERED=1

RUN addgroup --system exporter && adduser --system --ingroup exporter exporter

WORKDIR /app

COPY exporter.py /app/exporter.py
COPY requirements.txt /app/requirements.txt

# Install build deps, pip deps, then remove build deps in one RUN
# Use apt options to retry and reduce failures
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      apt-transport-https \
      gnupg \
      curl \
      build-essential \
      libpq-dev \
    || (apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=10 update && apt-get install -y --no-install-recommends ca-certificates apt-transport-https gnupg curl build-essential libpq-dev); \
    pip install --no-cache-dir -r /app/requirements.txt; \
    # remove build deps to keep image small
    apt-get remove -y build-essential; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*

# Set permissions and switch to non-root user
RUN chown -R exporter:exporter /app
USER exporter

EXPOSE 9410
ENTRYPOINT ["python", "/app/exporter.py"]
