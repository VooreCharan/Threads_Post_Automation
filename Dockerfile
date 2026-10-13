FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# Mount a persistent volume at /data so posted-article history and the refreshed token survive redeploys
ENV DB_PATH=/data/bot.db
# One worker only: the optional scheduler runs inside the process
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
