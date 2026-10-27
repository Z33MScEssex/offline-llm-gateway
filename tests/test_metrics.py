"""Stage 6 acceptance test: Prometheus shows request count, latency and errors from the gateway.

Sends one good and one refused request with a freshly named key, then asks Prometheus what it
scraped for that key name. A new name each run means the counts start from zero, so the test
can check exact numbers and is not fooled by earlier runs.
Run: `docker compose run --rm tests` (Prometheus is reachable from inside the stack).
Sources: LiteLLM Prometheus page (metric names and labels); Prometheus HTTP API docs, "Instant queries".
"""
import json  # parses Prometheus replies
import os  # reads PROMETHEUS_URL
import time  # waits between polls while Prometheus scrapes
import urllib.parse  # encodes the PromQL query into the URL
import urllib.request  # standard-library HTTP client
import uuid  # makes a key name no earlier run has used

import pytest  # test runner

from test_gateway import MODEL, master_key, post  # reuse the gateway helpers, no duplicate code

PROMETHEUS = os.environ.get("PROMETHEUS_URL", "http://172.30.0.20:9090")  # fixed address on the offline network
WAIT_S = 30  # scrape interval is 5 s; 30 s allows several missed scrapes before calling it a failure


def query(promql):
    # Ask Prometheus for the current value of an expression; return {label-set: value}
    url = PROMETHEUS + "/api/v1/query?" + urllib.parse.urlencode({"query": promql})  # GET /api/v1/query?query=...
    with urllib.request.urlopen(url, timeout=10) as f:  # send and wait
        reply = json.loads(f.read().decode("utf-8"))  # parse the JSON reply
    assert reply["status"] == "success", reply  # a bad query is a test bug, say so
    return [(r["metric"], float(r["value"][1])) for r in reply["data"]["result"]]  # value is [time, "number"]


def wait_for(promql, expected):
    # Poll until Prometheus has scraped the expected total, or give up after WAIT_S
    deadline = time.time() + WAIT_S
    while True:
        total = sum(v for _, v in query(promql))  # add up every matching series
        if total >= expected or time.time() > deadline:  # done, or out of time
            return total
        time.sleep(1)  # Prometheus scrapes every 5 s; check again shortly


@pytest.fixture(scope="module")
def traffic():
    # One good request and one refused request, both under a key name unique to this run
    alias = f"metrics-test-{uuid.uuid4().hex[:8]}"  # e.g. metrics-test-3f9a1c2b
    status, reply = post("/key/generate",  # create the key, as in test_gateway.py
                         {"models": [MODEL], "duration": "30min", "key_alias": alias}, master_key())
    assert status == 200, reply
    key = reply["key"]
    good = {"model": MODEL, "messages": [{"role": "user", "content": "Say hello."}], "max_tokens": 10}
    bad = {"model": "not-allowed", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
    assert post("/v1/chat/completions", good, key)[0] == 200  # counted as a success
    assert post("/v1/chat/completions", bad, key)[0] == 403  # counted as a failure: key not allowed that model
    return alias


def test_request_count_is_recorded(traffic):
    total = wait_for(f'litellm_proxy_total_requests_metric_total{{api_key_alias="{traffic}"}}', 2)
    assert total == 2  # both requests counted, good and refused


def test_errors_are_recorded(traffic):
    promql = f'litellm_proxy_failed_requests_metric_total{{api_key_alias="{traffic}",exception_status="403"}}'
    assert wait_for(promql, 1) == 1  # exactly the one refused request, with its reason


def test_latency_is_recorded(traffic):
    count = wait_for(f'litellm_request_total_latency_metric_count{{api_key_alias="{traffic}"}}', 1)
    seconds = sum(v for _, v in query(f'litellm_request_total_latency_metric_sum{{api_key_alias="{traffic}"}}'))
    assert count == 1  # the good request was timed (a refused one never reaches the model)
    assert 0 < seconds < 60  # a real duration, not zero and not absurd
