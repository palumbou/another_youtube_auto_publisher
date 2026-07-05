FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir boto3 webrtcvad

WORKDIR /app
COPY worker.py .

# scratch space for downloaded videos (Fargate ephemeral storage is mounted here)
RUN mkdir -p /work

ENTRYPOINT ["python", "worker.py"]
