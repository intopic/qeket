#!/bin/sh
# Prepare the volume, start dockerd for offline builds, then run the worker as nobody.
set -eu
mountpoint -q /data || { echo "missing Fly volume at /data" >&2; exit 1; }
tools=/data/platform-tools
if [ ! -x "$tools/rust/bin/cargo" ]; then
  echo '{"event":"installing_platform_tools"}'
  rm -rf "$tools.partial" /data/platform-tools.tbz
  curl -fsSL --retry 3 --retry-all-errors -o /data/platform-tools.tbz \
    https://github.com/anza-xyz/platform-tools/releases/download/v1.58/platform-tools-linux-x86_64.tar.bz2
  echo "7b82eb420b5aebd1e5590db0ae25c523eca06724d4238ae02139690535636c37  /data/platform-tools.tbz" | sha256sum -c - >/dev/null
  mkdir "$tools.partial"
  tar xjf /data/platform-tools.tbz -C "$tools.partial"
  rm /data/platform-tools.tbz
  test -x "$tools.partial/rust/bin/cargo"
  mv "$tools.partial" "$tools"
fi
mountpoint -q /sys/fs/cgroup || mount -t cgroup2 none /sys/fs/cgroup
mkdir -p /data/docker /data/jobs /data/snapshots /data/home /data/tmp
# Builds use --network none. The bridge is unnecessary, and apt-get in the
# builder image uses host networking.
dockerd --data-root /data/docker --bridge=none --iptables=false --ip6tables=false --group docker \
  >/data/dockerd.log 2>&1 &
for _ in $(seq 1 60); do
  docker info >/dev/null 2>&1 && break
  sleep 1
done
docker info >/dev/null 2>&1 || { tail -20 /data/dockerd.log >&2; exit 1; }
install -d -m 0755 /run/sshd
install -d -m 0700 /root/.ssh
umask 077
printf '%s\n' "$BUILDER_HOST_KEY" > /run/sshd/host_key
printf 'restrict,command="/usr/local/bin/build-one" %s\n' "$BUILDER_CLIENT_PUBKEY" > /root/.ssh/authorized_keys
unset BUILDER_HOST_KEY BUILDER_CLIENT_PUBKEY
exec /usr/sbin/sshd -D -e -h /run/sshd/host_key -p 2222 -o ListenAddress=fly-local-6pn \
  -o PermitRootLogin=forced-commands-only -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no
