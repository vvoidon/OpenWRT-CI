#!/usr/bin/env bash
# Keep lightweight health evidence alongside build reports; never print command arguments or credentials.
set -uo pipefail

while true; do
  printf '\n=== Runner health %s ===\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
  free -m || true
  df -Pm / /mnt || true
  printf 'Load: '
  cat /proc/loadavg || true
  awk '$1 == "oom_kill" { print "Kernel OOM kills:", $2 }' /proc/vmstat || true
  for counter in /sys/fs/cgroup/memory.events /proc/pressure/cpu /proc/pressure/memory /proc/pressure/io; do
    if [[ -r "$counter" ]]; then
      printf '%s:\n' "$counter"
      cat "$counter" || true
    fi
  done
  printf 'Largest processes (RSS in KiB; names only):\n'
  ps -eo pid,ppid,comm,rss --sort=-rss | head -n 12 || true
  curl --head --location --silent --show-error --max-time 5 \
    --output /dev/null --write-out 'GitHub probe: HTTP %{http_code}, total %{time_total}s\n' \
    https://github.com || true
  [[ ${1:-} == --once ]] && break
  sleep 30
done
