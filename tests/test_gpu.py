"""GPU route: the gateway sends qwen3-gpu to vLLM, with thinking off.

Runs only when GPU_EXPECTED=1, because the default stack has no GPU model server.
Run on a GPU machine: GPU_EXPECTED=1 python3 -m pytest tests/test_gpu.py -v
Uses the master key, so it also works on a gateway with no database (as on Colab).
"""
import os  # reads GPU_EXPECTED

import pytest  # test runner

from test_gateway import master_key, post  # same gateway address, key lookup and HTTP helper

pytestmark = pytest.mark.skipif(os.environ.get("GPU_EXPECTED") != "1",
                                reason="set GPU_EXPECTED=1 on a machine running vLLM")  # skip everywhere else


def ask(prompt):
    # One chat request to the GPU model through the gateway
    body = {"model": "qwen3-gpu", "messages": [{"role": "user", "content": prompt}], "max_tokens": 64}
    return post("/v1/chat/completions", body, master_key())


def test_gpu_model_answers_through_the_gateway():
    status, reply = ask("Say hello.")
    assert status == 200, reply  # the gateway reached vLLM and got an answer
    assert reply["choices"][0]["message"]["content"].strip()  # and it is not empty


def test_thinking_is_off_by_default():
    # With thinking on, Qwen3 writes a reasoning block first and runs into max_tokens.
    # Off, a five-word answer takes a handful of tokens (stage 2: 5 tokens).
    status, reply = ask("Say hello in five words.")
    assert status == 200, reply
    assert "<think>" not in reply["choices"][0]["message"]["content"]  # no reasoning block in the answer
    assert reply["usage"]["completion_tokens"] < 20, reply["usage"]  # short, as with thinking off
