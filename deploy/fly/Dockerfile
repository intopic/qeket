FROM python:3.12-slim
WORKDIR /worker
COPY scripts/devnet.py scripts/live_capture.py scripts/live_prestate.py scripts/fly_listener.py scripts/customer_admin.py scripts/worker_http.py scripts/worker_notify.py ./
COPY deploy/fly/start.sh /worker/start.sh
ENTRYPOINT ["sh", "/worker/start.sh"]
