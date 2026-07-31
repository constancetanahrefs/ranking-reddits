FROM python:3.12-slim

# psycopg2-binary ships wheels, but curl is handy for the healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# ONE worker, several threads. Scans and Discover run in background threads and
# hold in-memory job state, so multiple workers would route the status poll to a
# process that knows nothing about the running job.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", \
     "--timeout", "120", "app.main:app"]
