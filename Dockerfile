FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY lifebot lifebot
# База и скачанная модель Whisper живут на томе /data и переживают пересборку.
ENV DB_PATH=/data/assistant.db WHISPER_DIR=/data/models WHISPER_MODEL=small PYTHONUNBUFFERED=1
VOLUME /data
CMD ["python", "-m", "lifebot"]
