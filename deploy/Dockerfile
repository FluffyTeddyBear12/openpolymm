FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PMUS_DATA_DIR=/app/pmus/data \
    PMUS_LOGS_DIR=/app/pmus/logs

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pmus/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY pmus /app/pmus

VOLUME ["/app/pmus/data", "/app/pmus/logs"]

ENTRYPOINT ["python", "-m", "pmus"]
CMD ["run", "--mode", "shadow"]
