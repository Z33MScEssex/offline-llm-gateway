"""Stage 7: load test the gateway. The method and the pass rules are in docs/stage7_plan.md
(read the amendment at its end: it replaces the first version of the rules).

Usage:
  python3 scripts/load_test.py curve --parallel 1   # Part A/B: latency and throughput at 1..16 users
  python3 scripts/load_test.py burst                     # Part C: does rpm_limit hold under a burst
Needs the stack up (`docker compose up -d --wait`) and LITELLM_MASTER_KEY in .env.
Sources: Python docs, concurrent.futures "ThreadPoolExecutor Example" and threading "Barrier objects";
Prometheus HTTP API docs, "Instant queries".
"""
import argparse  # reads the sub-command and options
import concurrent.futures  # runs many requests at the same time on a pool of threads
import json  # request bodies, replies and the saved results
import math  # ceil, for the nearest-rank percentile
import pathlib  # file paths for .env and results/
import statistics  # median
import subprocess  # asks `docker stats` for memory use
import threading  # Barrier releases a burst of requests together
import time  # wall-clock and per-request timing
import urllib.error  # HTTPError carries the status code of a refused request
import urllib.parse  # encodes PromQL into the query URL
import urllib.request  # standard-library HTTP client

ROOT = pathlib.Path(__file__).resolve().parent.parent  # the repo root
GATEWAY = "http://172.30.0.10:4000"  # the gateway's fixed address on the offline network
PROMETHEUS = "http://172.30.0.20:9090"  # Prometheus's fixed address on the offline network
SCRAPE_WAIT_S = 7  # longer than the 5 s scrape interval, so Prometheus has the latest numbers
ROUNDS = 2  # each curve runs twice; the degradation point must agree in both
MODEL = "qwen3-cpu"  # the model name in litellm/config.yaml
LEVELS = [1, 2, 4, 8, 16]  # simultaneous users, fixed in the plan
PROMPT = "Count from 1 to 20, separated by spaces."  # same work for every request
MAX_TOKENS = 32  # caps answer length so requests cost about the same
TIMEOUT_S = 300  # a request slower than this counts as an error


def master_key():
    # The admin key from .env; only it may create the test keys
    for line in (ROOT / ".env").read_text().splitlines():  # read .env line by line
        if line.startswith("LITELLM_MASTER_KEY="):  # the admin key line
            return line.split("=", 1)[1].strip()  # everything after the first "="
    raise SystemExit("LITELLM_MASTER_KEY not found in .env")  # cannot run without it


def post(path, body, key):
    # One JSON POST; returns (status code, parsed reply). Never raises on 4xx/5xx or timeouts.
    req = urllib.request.Request(GATEWAY + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as f:  # send and wait
            return f.status, json.loads(f.read().decode("utf-8"))  # success
    except urllib.error.HTTPError as e:  # refused: keep the status code
        return e.code, {}
    except OSError as e:  # timeout or connection failure: no status code at all
        return 0, {"error": str(e)}


def new_key(alias=None, **limits):
    # A normal user key, so every request pays the real auth and database cost.
    # The alias labels its Prometheus series, so one key's numbers can be read on their own.
    body = {"models": [MODEL], "duration": "60min", **limits}
    if alias:
        body["key_alias"] = alias
    status, reply = post("/key/generate", body, master_key())
    assert status == 200, f"could not create a key: {status} {reply}"
    return reply["key"]


def one_request(key):
    # Send the fixed request; return (status, seconds taken, output tokens)
    body = {"model": MODEL, "messages": [{"role": "user", "content": PROMPT}],
            "max_tokens": MAX_TOKENS, "temperature": 0}  # temperature 0: same answer every time
    t0 = time.perf_counter()  # start the clock just before sending
    status, reply = post("/v1/chat/completions", body, key)
    seconds = time.perf_counter() - t0  # time until the whole reply arrived
    tokens = reply.get("usage", {}).get("completion_tokens", 0) if status == 200 else 0  # tokens written
    return status, seconds, tokens


def prom_value(promql):
    # Current value of a PromQL expression, summed over every matching series (0 if none yet)
    url = PROMETHEUS + "/api/v1/query?" + urllib.parse.urlencode({"query": promql})  # GET /api/v1/query
    with urllib.request.urlopen(url, timeout=10) as f:
        reply = json.loads(f.read().decode("utf-8"))
    return sum(float(r["value"][1]) for r in reply["data"]["result"])  # value is [time, "number"]


def prom_latency(alias):
    # Prometheus's running totals for this key: (total seconds, number of requests timed)
    time.sleep(SCRAPE_WAIT_S)  # let at least one scrape happen after the last request
    sel = f'{{api_key_alias="{alias}"}}'  # only this key's requests
    return (prom_value(f"litellm_request_total_latency_metric_sum{sel}"),
            prom_value(f"litellm_request_total_latency_metric_count{sel}"))


def ollama_parallel():
    # The OLLAMA_NUM_PARALLEL the running ollama container actually has
    out = subprocess.run(["docker", "compose", "exec", "-T", "ollama", "printenv", "OLLAMA_NUM_PARALLEL"],
                         cwd=ROOT, capture_output=True, text=True)
    return out.stdout.strip()


def p95(values):
    # Nearest-rank 95th percentile: the smallest value with at least 95% of values at or below it.
    # With fewer than 20 values this is the maximum; every level here has at least 40.
    s = sorted(values)
    return s[max(0, math.ceil(0.95 * len(s)) - 1)]


def ollama_memory():
    # Memory used by the ollama container right now, as docker reports it (e.g. "2.1GiB / 15.3GiB")
    out = subprocess.run(["docker", "compose", "stats", "--no-stream", "--format", "{{.MemUsage}}", "ollama"],
                         cwd=ROOT, capture_output=True, text=True)
    return out.stdout.strip()


def run_level(key, alias, users):
    # Part A, one level: `users` threads, max(40, 8*users) requests in total
    n = max(40, 8 * users)  # at least 40, so p95 is not just the slowest request
    for _ in range(2):  # 2 warm-up requests, not counted
        one_request(key)
    before = prom_latency(alias)  # Prometheus totals after the warm-ups, before the level
    t0 = time.perf_counter()  # wall clock from the first request sent...
    with concurrent.futures.ThreadPoolExecutor(max_workers=users) as pool:  # exactly `users` at once
        results = list(pool.map(lambda _: one_request(key), range(n)))  # queue n requests on the pool
    wall = time.perf_counter() - t0  # ...to the last reply
    memory = ollama_memory()  # straight after the level, while the model is still loaded
    after = prom_latency(alias)  # Prometheus totals after the level
    ok = [r for r in results if r[0] == 200]  # successful requests only
    lat = [r[1] for r in ok]  # their latencies
    prom_n = after[1] - before[1]  # requests Prometheus timed during this level
    return {  # unrounded: the rule compares these; rounding is only for display
        "users": users, "requests": n, "ok": len(ok), "errors": n - len(ok),
        "status_codes": sorted({r[0] for r in results}),  # every status seen, so an error type is visible
        "p50_s": statistics.median(lat) if lat else None,
        "p95_s": p95(lat) if lat else None,
        "p95_sample_size": len(lat),
        "requests_per_s": len(ok) / wall,  # completed requests over wall-clock time
        "tokens_per_s": sum(r[2] for r in ok) / wall,  # output tokens over wall-clock time
        "mean_tokens": sum(r[2] for r in ok) / len(ok) if ok else None,  # check the work stayed equal
        "wall_s": wall,
        "ollama_memory": memory,
        "client_mean_s": statistics.mean(lat) if lat else None,  # Part D: the client's view
        "prometheus_mean_s": (after[0] - before[0]) / prom_n if prom_n else None,  # Part D: the gateway's view
        "prometheus_count": prom_n,  # Part D check: must equal "ok"
        "prometheus_count_matches": prom_n == len(ok),  # False means the two views cover different requests
    }


def degradation_point(levels):
    # The rule fixed in docs/stage7_plan.md: the lowest level where throughput rises <10% over the
    # previous level while p95 rises >50%, or where any request fails.
    for prev, cur in zip([None] + levels[:-1], levels):
        if cur["errors"] > 0:  # any failed request
            return cur["users"], f"{cur['errors']} of {cur['requests']} requests failed"
        if prev and prev["p95_s"] and cur["p95_s"]:  # needs a previous level to compare against
            gain = cur["requests_per_s"] / prev["requests_per_s"] - 1  # throughput change
            slower = cur["p95_s"] / prev["p95_s"] - 1  # p95 latency change
            if gain < 0.10 and slower > 0.50:
                return cur["users"], f"throughput {gain:+.0%}, p95 latency {slower:+.0%} vs {prev['users']} users"
    return None, "no degradation up to the highest level tested"


def curve(parallel):
    actual = ollama_parallel()  # what the running container really has
    if actual != str(parallel):  # refuse to label a run with a setting it did not use
        raise SystemExit(f"ollama has OLLAMA_NUM_PARALLEL={actual!r}, not {parallel}. "
                         f"Restart with: OLLAMA_NUM_PARALLEL={parallel} docker compose up -d --wait")
    rounds = []
    for rnd in range(1, ROUNDS + 1):
        alias = f"load-p{parallel}-r{rnd}-{time.strftime('%H%M%S')}"  # new name: Prometheus totals start fresh
        key = new_key(alias)  # one unlimited key per round
        levels = []
        for users in LEVELS:
            r = run_level(key, alias, users)
            levels.append(r)
            print(json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()}), flush=True)
            if not r["prometheus_count_matches"]:  # say so loudly, never leave it buried in the JSON
                print(f"WARNING: Prometheus timed {r['prometheus_count']} requests, client saw {r['ok']} succeed. "
                      f"Part D comparison for {users} users is not like for like.", flush=True)
        point, why = degradation_point(levels)
        rounds.append({"round": rnd, "key_alias": alias, "levels": levels, "degradation_point": point, "why": why})
        print(f"round {rnd}: degradation point {point} ({why})", flush=True)
    points = {r["degradation_point"] for r in rounds}
    verdict = (f"degrades at {points.pop()} users" if len(points) == 1 and None not in points
               else "no degradation up to 16 users" if points == {None}
               else "not stable: rounds disagree " + str([r["degradation_point"] for r in rounds]))
    out = {"ollama_num_parallel": actual, "date": time.strftime("%Y-%m-%d %H:%M"), "prompt": PROMPT,
           "max_tokens": MAX_TOKENS, "rounds": rounds, "verdict": verdict}
    path = ROOT / "results" / f"stage7_load_parallel{parallel}.json"
    path.write_text(json.dumps(out, indent=2) + "\n")  # saved for the summary
    print(f"VERDICT: {verdict} -> {path.name}")


def burst(limit=5, size=20, repeats=3):
    # Part C: `size` requests released together on a key allowed `limit` per minute
    runs = []
    for i in range(repeats):
        key = new_key(rpm_limit=limit)  # fresh key, so earlier runs cannot use up its minute
        gate = threading.Barrier(size)  # every thread waits here until all `size` have arrived

        def fire(_):
            gate.wait()  # released together, so the requests really arrive at the same moment
            return one_request(key)[0]  # only the status code matters here

        with concurrent.futures.ThreadPoolExecutor(max_workers=size) as pool:
            codes = list(pool.map(fire, range(size)))
        r = {"run": i + 1, "limit": limit, "sent": size, "ok": codes.count(200), "refused_429": codes.count(429),
             "other": sorted(c for c in codes if c not in (200, 429))}
        if r["ok"] > limit:  # more got through than the key allows
            r["verdict"] = "leak"
        elif r["ok"] == limit and r["refused_429"] == size - limit:  # exactly the limit got through
            r["verdict"] = "holds"
        else:  # e.g. a 500 or a timeout: says nothing either way about the limit
            r["verdict"] = "unclear"
        runs.append(r)
        print(json.dumps(r), flush=True)
    path = ROOT / "results" / "stage7_burst.json"
    path.write_text(json.dumps({"date": time.strftime("%Y-%m-%d %H:%M"), "runs": runs}, indent=2) + "\n")
    counts = {v: sum(r["verdict"] == v for r in runs) for v in ("holds", "leak", "unclear")}
    print(f"bursts: {counts} -> {path.name}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Stage 7 load test")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("curve")
    c.add_argument("--parallel", type=int, required=True)  # the OLLAMA_NUM_PARALLEL this run expects
    sub.add_parser("burst")
    a = p.parse_args()
    curve(a.parallel) if a.cmd == "curve" else burst()
