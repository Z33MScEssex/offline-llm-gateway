"""Stage 3 acceptance tests: the LiteLLM gateway enforces keys and rate limits.

Needs the stack running (`docker compose up -d --wait`) and LITELLM_MASTER_KEY in .env.
Run: `docker compose run --rm tests` (in a container), or `python3 -m pytest tests/ -v` on the host.
Sources: LiteLLM docs, virtual keys page (/key/generate, "duration") and budgets/rate limits page
(/key/generate with "rpm_limit"); Python docs, urllib.request examples.
"""
import json  # turns request bodies into JSON text and replies back into dicts
import os  # reads the gateway address from the environment if it is set
import pathlib  # finds .env relative to this file, wherever pytest is run from
import urllib.error  # HTTPError carries the status code of a refused request
import urllib.request  # standard-library HTTP client

import pytest  # test runner

GATEWAY = os.environ.get("GATEWAY_URL", "http://172.30.0.10:4000")  # the gateway's fixed address on the offline network
MODEL = "qwen3-cpu"  # the model name set in litellm/config.yaml
ENV_FILE = pathlib.Path(__file__).resolve().parent.parent / ".env"  # the secrets file compose reads


def master_key():
    # Prefer the environment; fall back to .env so the tests run with no extra setup
    if os.environ.get("LITELLM_MASTER_KEY"):  # already exported in the shell
        return os.environ["LITELLM_MASTER_KEY"]
    for line in ENV_FILE.read_text().splitlines():  # read .env line by line
        if line.startswith("LITELLM_MASTER_KEY="):  # the admin key line
            return line.split("=", 1)[1].strip()  # everything after the first "="
    pytest.skip("LITELLM_MASTER_KEY not found in the environment or .env")  # cannot test without it


def post(path, body, key, timeout=120):
    # Send one JSON POST to the gateway and return (status code, parsed reply)
    req = urllib.request.Request(  # a Request with data set is sent as a POST
        url=GATEWAY + path,  # e.g. /v1/chat/completions
        data=json.dumps(body).encode("utf-8"),  # JSON body as bytes
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},  # key goes in the Bearer header
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as f:  # send and wait
            return f.status, json.loads(f.read().decode("utf-8"))  # success: status and body
    except urllib.error.HTTPError as e:  # urlopen raises on 4xx/5xx; the status is what the tests check
        return e.code, json.loads(e.read().decode("utf-8") or "{}")  # refused: status and error body


def chat(key):
    # One short chat request; max_tokens keeps each call to about a second on CPU
    body = {"model": MODEL, "messages": [{"role": "user", "content": "Say hello."}], "max_tokens": 10}
    return post("/v1/chat/completions", body, key)


def new_key(**limits):
    # Ask the gateway for a fresh user key, allowed only this model, expiring on its own
    body = {"models": [MODEL], "duration": "30min", **limits}  # duration: test keys clean themselves up
    status, reply = post("/key/generate", body, master_key())  # only the master key may create keys
    assert status == 200, f"could not create a key: {status} {reply}"  # fail loudly if the admin call broke
    return reply["key"]  # the new sk-... key


def test_valid_key_gets_a_reply():
    status, reply = chat(new_key())  # a key with no limits
    assert status == 200  # accepted
    assert reply["choices"][0]["message"]["content"].strip()  # and the model actually answered


def test_bad_key_is_refused():
    status, _ = chat("sk-this-key-was-never-issued")  # a key the gateway has never seen
    assert status == 401  # refused as unauthorised, and the model is never called


def test_key_cannot_use_a_model_it_was_not_given():
    key = new_key()  # allowed only MODEL
    body = {"model": "some-other-model", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
    status, _ = post("/v1/chat/completions", body, key)  # ask for a model outside its list
    assert status in (400, 401, 403)  # refused; the exact code is LiteLLM's choice, success is the failure


def test_going_over_the_rate_limit_gets_429():
    limit = 2  # requests per minute allowed for this key
    key = new_key(rpm_limit=limit)  # same field as the docs' /key/generate example
    statuses = [chat(key)[0] for _ in range(limit + 1)]  # one more request than the limit, back to back
    assert statuses[:limit] == [200] * limit  # everything up to the limit gets through
    assert statuses[limit] == 429  # the next one is refused as "too many requests"
