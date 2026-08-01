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
CMD ["uvicorn", "cloud_api:app", "--host", "0.0.0.0", "--port", "8001"]
