FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends docker.io git openssh-server ca-certificates curl bzip2 util-linux && rm -rf /var/lib/apt/lists/*
WORKDIR /worker
COPY scripts/worker_build.py .
COPY deploy/fly/verifier/build-one /usr/local/bin/build-one
COPY deploy/fly/verifier/start-builder.sh .
RUN chmod 755 /usr/local/bin/build-one
ENTRYPOINT ["sh", "/worker/start-builder.sh"]
