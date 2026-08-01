FROM python:3.12-slim

# tzdata so datetime.now() uses America/Toronto (matches the PC -- critical for
# reminders firing at the right local time)
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
ENV TZ=America/Toronto

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY cloud_api.py reminders_worker.py queue_store.py sent_store.py ./

ENV REMINDERS_DB=/data/reminders.db
ENV QUEUE_DB=/data/queue.db
EXPOSE 8001
# Loopback only. The container runs with network_mode: host, so 0.0.0.0 would
# publish this on the VM's public IP with nothing but a firewall rule in front
# of it. Every caller (n8n, the watchdog) is on this host and uses localhost.
CMD ["uvicorn", "cloud_api:app", "--host", "127.0.0.1", "--port", "8001"]
