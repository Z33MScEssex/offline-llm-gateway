"""Stage 2b: vLLM behind the LiteLLM gateway on a Colab GPU.

Starts vLLM (as in stage 2) and the gateway with this repo's litellm/config.yaml, runs
tests/test_gpu.py through the gateway, then runs the stage 7 load test twice in the same
session: straight to vLLM and through the gateway. The gap between the two is what the
gateway costs. Colab has no Docker and no database, so the gateway runs from pip with the
master key only. Per-user keys (a database lookup) are therefore not in the measured path.

Colab setup (run once, from the repo root on the runtime):
  pip install -q vllm==0.32.0
  pip install -q virtualenv && python -m virtualenv /content/llenv
  /content/llenv/bin/pip install -q "litellm[proxy]==1.90.2" prometheus_client
  python -u colab/gateway_gpu.py
Found on the 2026-10-27 run: Colab's Python has no ensurepip, so `python -m venv` fails; and
the pip package, unlike the Docker image, does not include prometheus_client, which the
config's Prometheus callback needs.
"""
import atexit  # stops the servers however the script ends
import concurrent.futures  # runs N requests at the same time
import json  # request bodies and the results file
import math  # ceil, for the nearest-rank p95
import os  # environment for the two servers
import secrets  # a throwaway master key for this run
import statistics  # median and mean
import subprocess  # starts vLLM, the gateway and pytest
import time  # timing
import urllib.request  # standard-library HTTP client

VLLM = "http://localhost:8000/v1/chat/completions"  # vLLM's OpenAI-style endpoint
GATEWAY = "http://localhost:4000"  # LiteLLM's default port (LiteLLM vLLM page: litellm --config ...)
PROMPT = "Count from 1 to 20, separated by spaces."  # identical to stages 2 and 7
MAX_TOKENS = 32
LEVELS = [1, 2, 4, 8, 16, 32, 64]  # same levels as stage 2
KEY = "sk-" + secrets.token_hex(16)  # master key for this run only; never written to disk


def wait_for(url, proc, name, headers=None):
    # Poll url until it answers, or stop if the server process has died
    t0 = time.time()
    while True:
        try:
            urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=2)
            return time.time() - t0  # seconds until ready
        except OSError:
            if proc.poll() is not None:  # the server exited: show the end of its log
                raise SystemExit(f"{name} exited, see {name}.log:\n" + open(f"{name}.log").read()[-3000:])
            time.sleep(3)


def one_request(via_gateway):
    # One timed request, either straight to vLLM or through the gateway
    if via_gateway:  # the gateway's model name; thinking off comes from the config, not the request
        url, headers = GATEWAY + "/v1/chat/completions", {"Authorization": f"Bearer {KEY}"}
        body = {"model": "qwen3-gpu"}
    else:  # vLLM directly, thinking switched off per request as in stage 2
        url, headers = VLLM, {}
        body = {"model": "Qwen/Qwen3-1.7B", "chat_template_kwargs": {"enable_thinking": False}}
    body.update({"messages": [{"role": "user", "content": PROMPT}], "max_tokens": MAX_TOKENS, "temperature": 0})
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    t0 = time.perf_counter()
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=300).read())
        return 200, time.perf_counter() - t0, r["usage"]["completion_tokens"]
    except OSError as e:  # HTTPError has .code; a dropped connection does not
        return getattr(e, "code", 0), time.perf_counter() - t0, 0


def p95(v):
    s = sorted(v)
    return s[max(0, math.ceil(0.95 * len(s)) - 1)]  # nearest rank, as in stages 2 and 7


def run_level(users, via_gateway):
    n = max(40, 8 * users)  # same request count rule as stages 2 and 7
    for _ in range(2):
        one_request(via_gateway)  # warm-ups, not counted
    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=users) as pool:
        res = list(pool.map(lambda _: one_request(via_gateway), range(n)))
    wall = time.perf_counter() - t0
    ok = [r for r in res if r[0] == 200]
    lat = [r[1] for r in ok]
    return {"users": users, "requests": n, "ok": len(ok), "errors": n - len(ok),
            "status_codes": sorted({r[0] for r in res}),
            "p50_s": statistics.median(lat) if lat else None, "p95_s": p95(lat) if lat else None,
            "requests_per_s": len(ok) / wall, "tokens_per_s": sum(r[2] for r in ok) / wall,
            "mean_tokens": sum(r[2] for r in ok) / len(ok) if ok else None, "wall_s": wall}


def degradation_point(levels):  # identical rule to scripts/load_test.py
    for prev, cur in zip([None] + levels[:-1], levels):
        if cur["errors"] > 0:
            return cur["users"], f"{cur['errors']} of {cur['requests']} requests failed"
        if prev and prev["p95_s"] and cur["p95_s"]:
            gain = cur["requests_per_s"] / prev["requests_per_s"] - 1
            slower = cur["p95_s"] / prev["p95_s"] - 1
            if gain < 0.10 and slower > 0.50:
                return cur["users"], f"throughput {gain:+.0%}, p95 latency {slower:+.0%} vs {prev['users']} users"
    return None, "no degradation up to the highest level tested"


vllm = subprocess.Popen(["vllm", "serve", "Qwen/Qwen3-1.7B", "--max-model-len", "4096"],
                        stdout=open("vllm.log", "w"), stderr=subprocess.STDOUT)
atexit.register(vllm.terminate)  # a failed step must not leave vLLM holding the GPU (it did on the first run)
print(f"vLLM ready after {wait_for('http://localhost:8000/v1/models', vllm, 'vllm'):.0f} s", flush=True)

env = {**os.environ, "LITELLM_MASTER_KEY": KEY, "VLLM_API_BASE": "http://localhost:8000/v1"}  # what compose sets
gateway = subprocess.Popen(["/content/llenv/bin/litellm", "--config", "litellm/config.yaml", "--port", "4000"],
                           env=env, stdout=open("gateway.log", "w"), stderr=subprocess.STDOUT)
atexit.register(gateway.terminate)
print(f"gateway ready after {wait_for(GATEWAY + '/health/readiness', gateway, 'gateway'):.0f} s", flush=True)

tests = subprocess.run(["python", "-m", "pytest", "tests/test_gpu.py", "-v", "-p", "no:cacheprovider"],
                       env={**env, "GPU_EXPECTED": "1", "GATEWAY_URL": GATEWAY}, capture_output=True, text=True)
print(tests.stdout[-1500:], flush=True)
if tests.returncode != 0:  # no point measuring a route that does not work
    raise SystemExit("GPU tests failed, load test not run")

# Same question both ways at temperature 0: the same token count shows the gateway passes the
# thinking-off setting on, so the two curves measure the same work.
_, _, n_direct = one_request(False)
_, _, n_gateway = one_request(True)
print(f"tokens direct {n_direct}, through gateway {n_gateway}", flush=True)
if n_direct != n_gateway or n_direct == 0:
    raise SystemExit("direct and gateway answers differ, comparison would not be fair")

curves = []
for rnd in (1, 2):
    for via_gateway in ((False, True) if rnd == 1 else (True, False)):  # order swaps in round 2, so neither always goes first
        levels = []
        for u in LEVELS:
            r = run_level(u, via_gateway)
            levels.append(r)
            print(json.dumps({"round": rnd, "gateway": via_gateway,
                              **{k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()}}), flush=True)
        point, why = degradation_point(levels)
        curves.append({"round": rnd, "via_gateway": via_gateway, "levels": levels,
                       "degradation_point": point, "why": why})
        print(f"round {rnd}, gateway={via_gateway}: {point} ({why})", flush=True)

gpu = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
                     capture_output=True, text=True).stdout.strip()
out = {"gpu": gpu, "vllm": "0.32.0", "litellm": "1.90.2", "model": "Qwen/Qwen3-1.7B", "prompt": PROMPT,
       "max_tokens": MAX_TOKENS, "tests_returncode": tests.returncode, "curves": curves}
open("stage2_gateway_gpu.json", "w").write(json.dumps(out))
print("RESULT " + json.dumps(out), flush=True)
gateway.terminate()
vllm.terminate()
