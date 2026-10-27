#!/usr/bin/env bash
# Stage 5: prove every running service is cut off from the internet, and that the probe itself works.
# Usage: scripts/check_offline.sh   (with the stack up: docker compose up -d --wait)
# Exit code 0 only if every service is blocked AND the control reached the internet.
set -u  # stop on any unset variable
cd "$(dirname "$0")/.."  # run from the repo root so docker compose finds the compose file

# bash can open a TCP connection by redirecting to /dev/tcp/HOST/PORT
PROBE='exec 3<>/dev/tcp/1.1.1.1/443'  # try to connect to Cloudflare on the HTTPS port
# Not every image has bash (Prometheus does not), and a probe that cannot start would look
# "blocked". So the probe always runs in this helper image, joined to the target's network.
HELPER=postgres:16  # already in the stack, has bash, so nothing extra to pull
fail=0  # becomes 1 if any check goes the wrong way

# Control: the same probe from a container on Docker's normal network must SUCCEED.
# If it fails, the probe is broken (or the laptop is offline) and "blocked" below would prove nothing.
if timeout 20 docker run --rm --entrypoint bash "$HELPER" -c "$PROBE" 2>/dev/null; then
  echo "control  normal network      reached the internet  (probe works)"
else
  echo "control  normal network      could NOT reach the internet: probe or laptop broken, result void"
  fail=1
fi

for svc in $(docker compose ps --services --status running); do  # every service that is up
  id=$(docker compose ps -q "$svc")  # the container's id
  # --network container:<id> puts the helper inside that container's own network stack
  out=$(timeout 20 docker run --rm --network "container:$id" --entrypoint bash "$HELPER" -c "$PROBE" 2>&1)
  rc=$?
  if [ "$rc" = 0 ]; then
    echo "FAIL     $svc  reached the internet"
    fail=1
  elif echo "$out" | grep -q "Network is unreachable"; then  # the one answer that means "blocked"
    echo "ok       $svc  cannot reach the internet (Network is unreachable)"
  else
    echo "FAIL     $svc  probe gave no clear answer: $out"  # never count an unclear probe as blocked
    fail=1
  fi
done

# Last: the laptop can still use the gateway, at its fixed address on the offline network
code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' http://172.30.0.10:4000/health/liveliness)
if [ "$code" = "200" ]; then
  echo "ok       laptop -> gateway   answers (HTTP 200)"
else
  echo "FAIL     laptop -> gateway   no answer (HTTP $code)"
  fail=1
fi

[ "$fail" = 0 ] && echo "PASS: offline, and the gateway still answers" || echo "FAIL: see lines above"
exit "$fail"
