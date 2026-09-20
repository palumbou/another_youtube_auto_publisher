FROM python:3.12-slim

# gcc only for the webrtcvad build, purged afterwards
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg gcc libc6-dev \
    && apt-get install -y --no-install-recommends fonts-noto-core \
    && pip install --no-cache-dir boto3 webrtcvad jsonschema \
    && apt-get purge -y --auto-remove gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY worker/worker.py .
COPY autopublisher ./autopublisher
COPY contracts ./contracts

# scratch space for downloaded videos (Fargate ephemeral storage is mounted here)
RUN mkdir -p /work

ENV PYTHONPATH=/app
ENTRYPOINT ["python", "worker.py"]
