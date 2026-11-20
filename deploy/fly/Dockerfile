FROM python:3.12-slim
WORKDIR /worker
COPY scripts/devnet.py scripts/live_capture.py scripts/fly_listener.py scripts/customer_admin.py ./
COPY deploy/fly/start.sh /worker/start.sh
ENTRYPOINT ["sh", "/worker/start.sh"]
