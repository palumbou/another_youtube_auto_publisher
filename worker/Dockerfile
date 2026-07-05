FROM python:3.12-slim

# gcc only for the webrtcvad build, purged afterwards
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg gcc libc6-dev \
    && pip install --no-cache-dir boto3 webrtcvad \
    && apt-get purge -y --auto-remove gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY worker.py .

# scratch space for downloaded videos (Fargate ephemeral storage is mounted here)
RUN mkdir -p /work

ENTRYPOINT ["python", "worker.py"]
