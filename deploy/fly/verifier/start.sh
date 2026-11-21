#!/bin/sh
set -eu
mountpoint -q /data || exit 1
: "${CANARY_BUILDER_HOST:?missing independent build host}"
install -d -o nobody -g nogroup /data/jobs /data/snapshots /run/canary
chown nobody:nogroup /data
umask 077
printf '%s\n' "$CANARY_BUILDER_SSH_KEY" > /run/canary/build_key
printf '%s\n' "$CANARY_BUILDER_KNOWN_HOSTS" > /run/canary/known_hosts
chown nobody:nogroup /run/canary/build_key /run/canary/known_hosts
unset CANARY_BUILDER_SSH_KEY CANARY_BUILDER_KNOWN_HOSTS
exec setpriv --reuid=nobody --regid=nogroup --clear-groups python -u /worker/fly_listener.py
