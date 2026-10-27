# Runbook

How to start the gateway, check it is healthy, read its metrics, and fix the failures most likely
to happen. This covers the Docker Compose stack. The commands and error messages below were run or
seen on it on 2026-10-26. The Kubernetes proof of concept is in `scripts/k8s_up.sh`.

## Addresses

| What | Where |
|---|---|
| Gateway (OpenAI-style API) | `http://172.30.0.10:4000` |
| Prometheus | `http://172.30.0.20:9090` |
| Ollama, database | No address outside the stack, on purpose |

These are fixed addresses on an internal Docker network. Only the machine running the stack can
reach them.

## Start and stop

```
docker compose up -d --wait        # returns once every service reports healthy
docker compose down                # stop; keys and usage history are kept
docker compose down -v             # stop AND delete the database and metrics history
```

First time only: create `.env` (see README). Keep it. A new `LITELLM_SALT_KEY` makes everything
already stored in the database unreadable.

## Is it healthy?

```
docker compose ps                  # every service should say (healthy)
docker compose run --rm tests      # 12 tests: gateway, metrics, offline
scripts/check_offline.sh           # every service cut off from the internet, gateway answering
curl http://172.30.0.10:4000/health/liveliness
```

## Reading the metrics

Open Prometheus at `http://172.30.0.20:9090` and paste a query:

| Question | Query |
|---|---|
| Requests per second, by status | `sum by (status_code) (rate(litellm_proxy_total_requests_metric_total[1m]))` |
| Errors per second, by reason | `sum by (exception_status) (rate(litellm_proxy_failed_requests_metric_total[1m]))` |
| 95th percentile latency (s) | `histogram_quantile(0.95, sum by (le) (rate(litellm_request_total_latency_metric_bucket[5m])))` |

What normal looks like on this laptop (stage 7): about 0.55 requests per second, about 1.8 s per
short answer for one user. Latency rising while requests per second stays flat means requests are
queueing, not failing (see "Slow answers" below). These show 0 or NaN right after a test run,
because `rate()` needs two readings on the same key.

## The three failures most likely to happen

### 1. A config change did nothing

**Symptom:** you edited `litellm/config.yaml` (or `prometheus/prometheus.yml`) and nothing
changed. Seen on 2026-10-26: Prometheus kept getting 404 from `/metrics` after the metrics setting
was added.
**Why:** `docker compose up -d` restarts a container only when its own settings change. A change
inside a mounted file is not one.
**Fix:** `docker compose restart litellm` (or `prometheus`).

### 2. The stack will not start: "Pool overlaps"

**Symptom:**
```
failed to create network offline_llm_gateway_offline: Error response from daemon: invalid pool
request: Pool overlaps with other one on this address space
```
**Why:** another Docker network already uses `172.30.0.0/24`. Usually a second copy of this stack
(for example a test clone in another folder) is still running.
**Fix:** `docker network ls`, then stop whichever copy owns that range (`docker compose down` in
its folder). If something else needs that range permanently, change the subnet and the two fixed
addresses in `docker-compose.yml`, `scripts/` and the tests together.

### 3. Ollama will not start: "read-only file system"

**Symptom:** `dependency ollama failed to start`, and in `docker compose logs ollama`:
```
Error: mkdir /root/.ollama/models/blobs: read-only file system
```
**Why:** the model folder is mounted read-only and the folder Docker found is empty or wrong, so
Ollama tries to create its layout and cannot.
**Fix:** check where the models really are (`ollama list` on the host, then the folder
`/usr/share/ollama/.ollama/models` on Linux). Set `OLLAMA_MODELS_DIR` in `.env` to that folder, then
`docker compose up -d --wait`. Confirm with `docker compose exec ollama ollama list`.

## Other things worth knowing

- **Slow answers under load.** Ollama works on one request at a time by default, so with N users
  each waits for the N in front. Stage 7 measured flat throughput and latency growing with users.
  Setting `OLLAMA_NUM_PARALLEL=4` (in `.env` or the command line) gave 1.5x the throughput for
  1.3 GB more RAM.
- **Very slow, very long answers.** Qwen3 "thinks" out loud by default (29 s instead of 0.75 s for a
  short answer). The gateway turns it off (`think: false` in `litellm/config.yaml`). If answers start
  with a long reasoning block, that line has been lost.
- **401 from the gateway.** The key is wrong or expired (test keys last 30 to 60 minutes). Make a new
  one with the master key: `POST /key/generate`.
- **429 from the gateway.** That key's requests-per-minute limit is working. Wait a minute, or give
  the key a higher `rpm_limit`.
- **500 for `qwen3-gpu`: "Cannot connect to host vllm:8000".** The GPU model server is not
  running. It only starts on a machine with an NVIDIA GPU, with `docker compose --profile gpu up -d --wait`.
  On the laptop, use `qwen3-cpu`. Seen on 2026-10-27; the error took about 5 s to come back.
- **`check_offline.sh` says "control ... could NOT reach the internet".** The laptop itself is
  offline, so the check cannot prove anything. It is not a failure of the stack.
