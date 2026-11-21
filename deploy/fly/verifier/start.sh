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
chown 65534:65534 /data /data/jobs /data/snapshots /data/home /data/tmp
export PATH="$tools/rust/bin:$PATH" CANARY_PLATFORM_TOOLS="$tools" HOME=/data/home \
  CARGO_HOME=/data/home/.cargo TMPDIR=/data/tmp
# Docker socket access is needed to start the isolated build container.
exec python -u -c 'import grp, os, runpy
os.setgroups([grp.getgrnam("docker").gr_gid])
os.setgid(65534)
os.setuid(65534)
runpy.run_module("fly_listener", run_name="__main__")'
