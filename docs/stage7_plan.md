# Stage 7 plan: load test

Written 2026-10-26, before any load run. The rules below are fixed now so the results cannot
choose them.

## Questions

1. How do latency and throughput change as more users call the gateway at the same time?
2. At what number of simultaneous users does it degrade?
3. Does the per-key rate limit still hold when requests arrive all at once, rather than one by one?

## Setup

- The full offline stack (`docker compose up -d --wait`), qwen3:1.7b on CPU (12 threads, 16 GB RAM).
- Client: `scripts/load_test.py`, standard-library Python on the laptop, calling the gateway at
  `http://172.30.0.10:4000` through a normal virtual key with no limits. So every request pays
  the real cost of auth, the database and logging, not a master-key shortcut.
- The client runs on the same machine as the model. Its threads only wait on sockets, so its CPU
  use is small, but it is not zero. Stated as a limitation, not corrected for.
- Every request: the same prompt ("Count from 1 to 20, separated by spaces."), `max_tokens` 32,
  `temperature` 0, thinking off (the gateway default). Same prompt and a length cap keep the work
  per request roughly equal, so changes in latency come from load, not from the answer length.
- Per-request timeout 300 s. A timeout counts as an error.

## Part A: the load curve

- Simultaneous users (concurrency): 1, 2, 4, 8, 16.
- At each level, `max(8, 4 × concurrency)` requests, so every simultaneous user sends at least 4.
- Before each level, 2 warm-up requests that are not counted, so model loading never lands in a level.
- Recorded per level: median (p50) and 95th percentile (p95) latency, requests per second,
  output tokens per second (from the `usage` field), error count.
- Throughput = requests completed ÷ wall-clock time from the first request sent to the last reply.

**Prediction, from the Ollama FAQ:** `OLLAMA_NUM_PARALLEL` defaults to 1, and the container log
confirms 1. Ollama then works on one request at a time and queues the rest. So throughput should
be flat from concurrency 1, and latency should grow roughly in line with the number of users waiting.

**Degradation rule, fixed now:** the degradation point is the lowest concurrency level where
throughput rises by less than 10% over the previous level while p95 latency rises by more than 50%,
or where more than 1% of requests fail. If neither happens up to 16, the answer is "no degradation
up to 16", not a guess beyond it.

## Part B: one setting changed

Rerun Part A identically with `OLLAMA_NUM_PARALLEL=4`, one variable changed. The question is whether
letting Ollama work on 4 requests at once raises total throughput on this CPU, or only splits the
same compute four ways. The same degradation rule applies. Memory is recorded, because the FAQ says
RAM scales with `OLLAMA_NUM_PARALLEL × OLLAMA_CONTEXT_LENGTH`.

## Part C: the rate limit under a burst

- A fresh key with `rpm_limit` 5. Fire 20 requests at the same moment (20 threads, released together).
- Pass: exactly 5 succeed and 15 get 429. More than 5 successes means the limit leaks under a burst.
- Repeated 3 times, each with a fresh key, because a race may not show every time.

## Part D: cross-check

Compare the client's own latency with Prometheus's `litellm_request_total_latency_metric` for the
same run. They measure different things (the client includes the network hop and queueing in front
of the gateway), so they should be close, with the client's slightly higher. A big gap means one of
them is measuring wrong.

## Output

`results/stage7_load_parallel1.json`, `results/stage7_load_parallel4.json`, `results/stage7_burst.json`,
and a plain-words summary in `results/stage7_load.txt`.

## Amendment 2026-10-26, before any load run (after a second review)

A read-only second review found seven holes. All were checked against the code and accepted.
These replace the rules above where they differ.

1. **Part B now really changes the setting.** `docker-compose.yml` passes
   `OLLAMA_NUM_PARALLEL` to Ollama (default 1). The script reads the value back from the running
   container and records it, and refuses to run if it does not match the label.
2. **More requests per level:** `max(40, 8 × concurrency)`, so 40, 40, 40, 64, 128. With 40 or more,
   nearest-rank p95 is the 38th of 40, not the single slowest request. Sample size is saved with
   every percentile.
3. **Two rounds per curve.** The degradation point is reported only if both rounds give the same
   level. If they differ, the result is "not stable", with both levels shown. Comparisons use
   unrounded values.
4. **Errors:** the rule is "any failed request", stated plainly, because 1% of 40 is less than one.
5. **Burst verdict split three ways:** *leak* if more than 5 succeed; *holds* if exactly 5 succeed
   and 15 get 429; *unclear* for anything else (for example a 500 or a timeout), reported
   separately and not counted as either.
6. **Part D has a method.** Each level reads the key's
   `litellm_request_total_latency_metric_sum` and `_count` from Prometheus just before the level
   and again after it, each time waiting 7 s (more than one 5 s scrape) first. The difference
   covers exactly that level's requests, so the client and Prometheus see the same population.
   Check: the count difference must equal the level's successful requests. Compared: client mean
   latency vs Prometheus mean (sum ÷ count). Expected: client slightly higher, since it includes
   the hop to the gateway.
