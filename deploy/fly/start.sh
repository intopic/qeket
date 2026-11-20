#!/bin/sh
set -eu
exec python -u -c 'import os, runpy
if not os.path.ismount("/data"):
    raise SystemExit("missing Fly volume at /data")
os.chown("/data", 65534, 65534)
os.setgroups([])
os.setgid(65534)
os.setuid(65534)
runpy.run_module("fly_listener", run_name="__main__")'
