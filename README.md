# Offline LLM Gateway

**Status: in progress.** Started October 2026. This README is updated as each stage is built.

A self-hosted large language model (LLM) service that works with no route to the internet.
Models are served locally, sit behind a gateway that controls who can use them and how much,
and everything is packaged so it can be rebuilt from this repo in one command.

## Why I'm building it

Most LLM demos call a cloud API. A lot of real organisations can't do that: defence, government
and regulated industries often run on networks with no internet access at all. This project is
about the platform layer that makes a model usable in that setting:

- serving a model so engineers can call it like any other API
- putting a gateway in front of it with keys, limits and logging
- watching how it performs, and knowing when it's struggling
- proving the whole thing really does work offline

## Planned architecture

```
 client (curl, Python, app)
        |
        v
 LiteLLM gateway  --->  per-user keys, rate limits, usage logs
        |
        +----> Ollama   (small model on CPU, local laptop)
        +----> vLLM     (GPU, tested on Google Colab)
        |
 Prometheus  <---- metrics from the gateway and model servers
```

All services run in Docker on an internal network with no outbound route.

## Stages

| # | Stage | Status |
|---|---|---|
| 1 | Serve a small model locally with Ollama and call its API | Done 2026-10-26 |
| 2 | Serve a model with vLLM on a GPU (Colab) and measure GPU use | Done 2026-10-27 on a Colab T4, directly and behind the gateway (`qwen3-gpu`) |
| 3 | Put a LiteLLM gateway in front: routing, per-user keys, rate limits | Done 2026-10-26 |
| 4 | Package everything with Docker Compose | Done 2026-10-26 |
| 5 | Make it offline: internal network, pre-loaded model weights, a test that proves no outbound traffic | Done 2026-10-26 |
| 6 | Monitoring: Prometheus metrics for latency, throughput and errors | Done 2026-10-26 |
| 7 | Load test: how many requests before latency degrades, and does the rate limit hold | Done 2026-10-26 |
| 8 | CI: GitHub Actions runs the tests and builds the images on every push | Done 2026-10-27: passes on GitHub in about 3 minutes |
| 9 | Stretch: run the same stack on a local Kubernetes cluster (kind or k3d) | Done 2026-10-26 as a local proof of concept (limits listed in results) |
| 10 | Runbook: how to start, check, troubleshoot and recover the service | Done 2026-10-26 (`docs/runbook.md`) |

## Running it

Needs Docker with the Compose plugin, and Ollama with `qwen3:1.7b` already pulled on the host
(the container reads the host's model files read-only, so nothing is downloaded at start-up).

```
printf 'LITELLM_MASTER_KEY=sk-%s\nLITELLM_SALT_KEY=sk-%s\n' "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" > .env
docker compose up -d --wait
docker compose run --rm tests
scripts/check_offline.sh
```

On a machine with an NVIDIA GPU, `docker compose --profile gpu up -d --wait` also starts vLLM,
and the gateway serves it as `qwen3-gpu` next to `qwen3-cpu`. It needs the vLLM image and the
model's Hugging Face files already on the machine (`HF_CACHE_DIR`). This route was tested on a
Colab T4 (`colab/gateway_gpu.py`); the Compose service itself has not been run, because the
laptop has no NVIDIA GPU and Colab has no Docker.

The model folder defaults to `/usr/share/ollama/.ollama/models`, where a Linux Ollama install
keeps it. Point `OLLAMA_MODELS_DIR` somewhere else if yours lives elsewhere.

The tests run in their own container on the stack's network, so the host needs no Python.
The first run builds that small image.

The gateway answers at `http://172.30.0.10:4000` and speaks the OpenAI API.
Prometheus is at `http://172.30.0.20:9090`. Useful queries:

| Question | PromQL |
|---|---|
| Requests per second, by status code | `sum by (status_code) (rate(litellm_proxy_total_requests_metric_total[1m]))` |
| Errors per second, by reason | `sum by (exception_status) (rate(litellm_proxy_failed_requests_metric_total[1m]))` |
| 95th percentile latency, seconds | `histogram_quantile(0.95, sum by (le) (rate(litellm_request_total_latency_metric_bucket[5m])))` |

These show 0 (or `NaN` for latency) after a test run, because each test makes a new key and
`rate()` needs two scraped points on a series to see a change. Under steady traffic they fill in:
12 requests on one key over about 30 s gave 0.19 good requests per second, 0.04 refused, and a
95th percentile of 0.49 s.

`/metrics` on the gateway needs a key (LiteLLM's default). Prometheus gets it as a Compose
secret made from `.env`, so it is never written into the repo.

## CI

`.github/workflows/ci.yml` runs the same commands on every push. GitHub's machine has no Ollama,
so the workflow downloads the model once into `ci_models/` and points `OLLAMA_MODELS_DIR` at it.
That folder is cached, so later runs skip the download. This is the one online step, and it
stands in for carrying the weights to an offline box.

## Kubernetes (local proof of concept)

`scripts/k8s_up.sh` runs the same stack on a one-node [kind](https://kind.sigs.k8s.io/) cluster and
runs the gateway and offline tests inside it as a Job. Every image is loaded from the laptop, so the
cluster pulls nothing. Offline there means a NetworkPolicy plus a cluster DNS that does not forward
outside names. One gap was measured and is recorded in `results/stage9_kubernetes.txt`: a new pod
is open for under a second before the policy reaches it. Remove with `kind delete cluster --name llm-gateway`.

## How it stays offline

Every service sits on one Docker network marked `internal`, which has no route off the machine.
No container can reach the internet, the local network, or outside DNS. The model weights come
from the host's Ollama folder, mounted read-only, so nothing needs downloading.

Docker cannot publish a port from an internal network, so the gateway has no `localhost` port.
Instead it has a fixed address on that network. The laptop can reach it there, because Docker
lets the host into an internal network, but nothing beyond the laptop can. Ollama and the database
have no address the laptop uses, so every request goes through the gateway's keys and limits.

`scripts/check_offline.sh` proves it each time: it first shows the same probe *does* reach the
internet from a normal container (so the probe works), then shows every service in the stack
cannot, then shows the gateway still answers.

**Not covered yet:** the Docker images themselves are pulled from the internet once, on first
start. A fully air-gapped machine would need them carried across with `docker save` and
`docker load`.

## Results

Measured numbers only. Nothing is listed until it has been run.

| Stage | What was measured | Result |
|---|---|---|
| 1 | qwen3:1.7b on CPU, Ollama direct, warm | 18.6 tokens per second, 2.2 s for a one-sentence answer |
| 2 | Same model with vLLM on a Colab T4 GPU, same load test, 1 to 64 users | No degradation up to 64 users in either round. 1 user: 0.57 s per answer, 55 tokens/s (about 3x the laptop CPU). 64 users: 51.6 req/s with p95 1.3 s. At 16 users, 39x the CPU's throughput, because vLLM batches waiting requests into one pass |
| 2 | Same load test through the gateway, same session, 2 rounds each way | No cost at 1 to 2 users. From 4 users: +30 to 70 ms per typical answer, 6 to 11% less throughput. Slowest 5% wait up to 0.47 s longer at 64 users (1.72 s vs 1.25 s). 4,320 requests, 0 errors |
| 2 | vLLM on a T4, model shipped in bf16 | The T4 cannot run bf16; vLLM switched to fp16 by itself, no flag needed |
| 3 | Gateway acceptance tests | 4 of 4 pass: valid key answered, bad key 401, wrong model refused, over the limit 429 |
| 3 | Rate limit, key set to 2 requests per minute | 200, 200, then 429 |
| 3 | Qwen3 thinking on vs off, same five-word prompt | 464 tokens in 29 s vs 7 tokens in 0.75 s, so it is off by default |
| 4 | Clean copy to passing tests, three commands | 60 s cold (builds the test image), 39 s warm |
| 5 | Every service tries to reach the internet | db, litellm, ollama all blocked ("Network is unreachable"); the same probe from a normal container gets through |
| 5 | Tests run from inside the offline network | 9 of 9 pass: the 4 gateway tests plus 5 offline tests (3 public addresses, outside DNS, internal DNS as control) |
| 5 | Offline tests run on a normal network instead | 5 of 5 fail, so they really detect internet access |
| 6 | One good + one refused request, then ask Prometheus | 2 requests counted, 1 failure with reason 403, 1 latency sample (2.6 s) |
| 6 | Full suite inside the offline stack | 12 of 12 pass; clean copy to passing in 66 s |
| 6 | Offline check with a leak planted on purpose | Caught: "prometheus reached the internet", exit 1 |
| 7 | Load curve, Ollama default (1 request at a time), 1 to 16 users | Flat at 0.55 req/s (18 tokens/s). Degrades at 2 users; p95 grows from 2 s to 32 s at 16 users. No errors, requests queue |
| 7 | Same curve, `OLLAMA_NUM_PARALLEL=4` | 0.84 req/s, about 1.5x. Degrades at 8 users; p95 at 16 users 21 s. Costs 1.3 GB more RAM |
| 7 | 20 requests at once on a key allowed 5 per minute, 3 times | Exactly 5 through and 15 refused, every time |
| 7 | Client latency vs Prometheus, same requests | Within 0.03 s at every level, so the gateway metric can be trusted |
| 9 | Same tests inside a kind Kubernetes cluster | 9 of 9 pass (4 gateway, 5 offline) |
| 9 | NetworkPolicy alone | Blocks connections out, but pods could still resolve outside names through the cluster DNS. Fixed by turning off CoreDNS forwarding |
| 9 | Brand-new pod connecting at once | Reached the internet; after 1 s, blocked. A startup gap in policy enforcement, recorded, not fixed |

Raw output is in `results/`.

## Hardware

- Laptop: Linux (Pop!_OS), 12 CPU threads, 16 GB RAM, no NVIDIA GPU. Used for the CPU path.
- Google Colab GPU: used for the vLLM and GPU measurement stages.
