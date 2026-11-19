FROM python:3.12-slim
WORKDIR /worker
COPY scripts/devnet.py scripts/live_capture.py ./
USER 65534
ENTRYPOINT ["python", "-u", "live_capture.py", "watch"]
