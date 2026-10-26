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
| 1 | Serve a small model locally with Ollama and call its API | Not started |
| 2 | Serve a model with vLLM on a GPU (Colab) and measure GPU use | Not started |
| 3 | Put a LiteLLM gateway in front: routing, per-user keys, rate limits | Not started |
| 4 | Package everything with Docker Compose | Not started |
| 5 | Make it offline: internal network, pre-loaded model weights, a test that proves no outbound traffic | Not started |
| 6 | Monitoring: Prometheus metrics for latency, throughput and errors | Not started |
| 7 | Load test: how many requests before latency degrades, and does the rate limit hold | Not started |
| 8 | CI: GitHub Actions runs the tests and builds the images on every push | Not started |
| 9 | Stretch: run the same stack on a local Kubernetes cluster (kind or k3d) | Not started |
| 10 | Runbook: how to start, check, troubleshoot and recover the service | Not started |

## Results

Measured numbers go here as each stage is finished. Nothing is listed until it has been run.

## Hardware

- Laptop: Linux (Pop!_OS), 12 CPU threads, 16 GB RAM, no NVIDIA GPU. Used for the CPU path.
- Google Colab GPU: used for the vLLM and GPU measurement stages.
