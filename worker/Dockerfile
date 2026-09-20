FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && apt-get install -y --no-install-recommends fonts-noto-core \
    && pip install --no-cache-dir boto3 jsonschema \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY worker/worker.py .
COPY autopublisher ./autopublisher
COPY contracts ./contracts

# scratch space for downloaded videos (Fargate ephemeral storage is mounted here)
RUN mkdir -p /work

ENV PYTHONPATH=/app
ENTRYPOINT ["python", "worker.py"]
