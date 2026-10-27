#!/usr/bin/env bash
# Stage 9: run the stack on a local kind cluster and run the acceptance tests inside it.
# Usage: scripts/k8s_up.sh       Remove: kind delete cluster --name llm-gateway
# Needs: kind, Docker, .env (as for compose). kubectl is not needed on the laptop: the kind node
# ships its own, so every kubectl call runs inside the node.
set -euo pipefail  # stop on any error, unset variable or failed pipe stage
cd "$(dirname "$0")/.."  # repo root
CLUSTER=llm-gateway
NODE=$CLUSTER-control-plane  # kind names the node container after the cluster
k() { docker exec -i "$NODE" kubectl --kubeconfig=/etc/kubernetes/admin.conf "$@"; }  # kubectl inside the node

if ! kind get clusters | grep -qx "$CLUSTER"; then
  kind create cluster --name "$CLUSTER" --config k8s/kind-config.yaml  # one node, model folder mounted
fi

# Every image comes from the laptop, so the cluster never pulls from the internet (imagePullPolicy: Never)
docker compose build tests >/dev/null  # make sure the test image exists
# Not `kind load docker-image`: with Docker's containerd image store it fails with "content digest
# ... not found" (kind known issues page, kind#3795). The documented workaround saves only the
# platform the node needs and loads that archive instead.
tmp=$(mktemp -d)  # holds the image archives for a moment
trap 'rm -rf "$tmp"' EXIT  # removed however the script ends
for img in postgres:16 ollama/ollama:0.30.6 docker.litellm.ai/berriai/litellm:v1.90.2 offline_llm_gateway-tests:latest; do
  docker image save --platform linux/amd64 --output "$tmp/image.tar" "$img"
  kind load image-archive "$tmp/image.tar" --name "$CLUSTER"
done

# Config and test files the cluster needs, copied into the node. .env is NOT copied: the Secret
# is built below on the laptop and streamed in, so the keys never touch the node's disk.
# Not /tmp: the node mounts it as tmpfs, which docker cp cannot see into.
docker exec "$NODE" rm -rf /var/lib/gw && docker exec "$NODE" mkdir -p /var/lib/gw
docker cp litellm/config.yaml "$NODE":/var/lib/gw/config.yaml
docker cp tests "$NODE":/var/lib/gw/tests

k apply -f - < k8s/stack.yaml  # namespace, offline policy, deployments, services

# Offline DNS: CoreDNS forwards any name outside the cluster to the internet ("forward . /etc/resolv.conf",
# Kubernetes "Customizing DNS Service" page). That lets a pod resolve pypi.org even with egress blocked,
# and DNS can carry data out. Remove the forward block so only cluster names resolve. Cluster-wide,
# so it suits only a cluster meant to resolve nothing outside itself, like this local one.
# Measured 2026-10-26: without it test_public_names_do_not_resolve fails.
k -n kube-system get configmap coredns -o json | python3 -c '
import json, re, sys
cm = json.load(sys.stdin)
cf = cm["data"]["Corefile"]
cm["data"]["Corefile"] = re.sub(r"\n\s*forward \. /etc/resolv\.conf(\s*\{[^}]*\})?", "", cf)  # drop the block
print(json.dumps(cm))' | k replace -f - >/dev/null
k -n kube-system rollout restart deployment/coredns >/dev/null  # apply now, not at the next reload
k -n kube-system rollout status deployment/coredns --timeout=120s
k -n llm-gateway delete configmap litellm-config tests --ignore-not-found
k -n llm-gateway delete secret litellm-env --ignore-not-found
k -n llm-gateway create configmap litellm-config --from-file=/var/lib/gw/config.yaml
k -n llm-gateway create configmap tests --from-file=/var/lib/gw/tests/test_gateway.py --from-file=/var/lib/gw/tests/test_offline.py
# The Secret's text form (stringData, Kubernetes Secrets page) built from .env on the laptop and
# piped straight to kubectl. The keys are then stored in the cluster as a Secret.
python3 -c '
import json, sys
env = dict(l.split("=", 1) for l in open(".env").read().splitlines() if "=" in l and not l.startswith("#"))
keys = {k: env[k] for k in ("LITELLM_MASTER_KEY", "LITELLM_SALT_KEY")}
print(json.dumps({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "litellm-env", "namespace": "llm-gateway"},
                  "type": "Opaque", "stringData": keys}))' | k create -f - >/dev/null
k -n llm-gateway rollout restart deployment/litellm >/dev/null  # pick up the config if it changed

k -n llm-gateway rollout status deployment/db --timeout=300s
k -n llm-gateway rollout status deployment/ollama --timeout=300s
k -n llm-gateway rollout status deployment/litellm --timeout=300s

k -n llm-gateway delete job tests --ignore-not-found >/dev/null
k apply -f - < k8s/tests-job.yaml
# Wait for the Job to finish either way, then show its output and pass on its result
for i in $(seq 1 120); do
  s=$(k -n llm-gateway get job tests -o jsonpath='{.status.succeeded}{.status.failed}')
  [ -n "$s" ] && break
  sleep 2
done
k -n llm-gateway logs job/tests
[ "$(k -n llm-gateway get job tests -o jsonpath='{.status.succeeded}')" = "1" ] && echo "PASS: tests passed inside the cluster" || { echo "FAIL: tests failed inside the cluster"; exit 1; }
