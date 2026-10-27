"""Stage 1: call a model served by Ollama over HTTP and report how fast it answered.

Usage: python3 scripts/call_model.py ["your prompt"] [--model qwen3:1.7b] [--url http://localhost:11434]
Sources: Ollama API docs, "Chat request (No streaming)"; Python docs, urllib.request examples.
"""
import argparse  # reads the command-line options
import json  # turns the request into JSON text and the reply back into a dict
import sys  # lets the script exit with a failure code
import urllib.error  # the error types urlopen raises when the server can't be reached
import urllib.request  # standard-library HTTP client, so no third-party install is needed


def chat(url, model, prompt, timeout=300):
    # Build the body exactly as the Ollama docs' non-streaming chat example does
    body = {
        "model": model,  # which local model answers
        "messages": [{"role": "user", "content": prompt}],  # one user turn, no chat history
        "stream": False,  # one JSON reply instead of a stream of partial ones
        "think": False,  # Qwen3 reasons out loud by default; off keeps replies short and timings comparable
    }
    data = json.dumps(body).encode("utf-8")  # urllib sends bytes, so encode the JSON text
    req = urllib.request.Request(  # a Request with data set is sent as a POST
        url=url.rstrip("/") + "/api/chat",  # Ollama's chat endpoint
        data=data,  # the encoded JSON body
        headers={"Content-Type": "application/json"},  # tell the server the body is JSON
    )
    with urllib.request.urlopen(req, timeout=timeout) as f:  # send it and wait, up to the timeout
        return json.loads(f.read().decode("utf-8"))  # read the whole reply and parse it


def main():
    p = argparse.ArgumentParser(description="Call an Ollama model once and report speed.")  # option parser
    p.add_argument("prompt", nargs="?", default="In one sentence, what is an API gateway?")  # optional prompt
    p.add_argument("--model", default="qwen3:1.7b")  # the model chosen in research Topic 0
    p.add_argument("--url", default="http://localhost:11434")  # Ollama's default address and port
    a = p.parse_args()  # read what was typed

    try:
        r = chat(a.url, a.model, a.prompt)  # make the call
    except urllib.error.URLError as e:  # server down, wrong port, or refused connection
        sys.exit(f"FAIL: could not reach Ollama at {a.url} ({e.reason}). Is `ollama serve` running?")

    ns = 1e9  # Ollama reports durations in nanoseconds; this converts them to seconds
    total_s = r["total_duration"] / ns  # whole request, including loading the model if it was cold
    load_s = r["load_duration"] / ns  # time spent loading the model into memory
    gen_s = r["eval_duration"] / ns  # time spent writing the answer
    tokens = r["eval_count"]  # number of tokens in the answer
    tps = tokens / gen_s if gen_s else 0.0  # generation speed in tokens per second

    print(r["message"]["content"].strip())  # the answer itself
    print("---")  # separator between the answer and the timings
    print(f"model: {r['model']}")  # confirms which model actually answered
    print(f"total time: {total_s:.2f} s (of which loading the model: {load_s:.2f} s)")  # end-to-end time
    print(f"answer: {tokens} tokens in {gen_s:.2f} s = {tps:.1f} tokens per second")  # speed in words
    print("PASS: got a reply over HTTP")  # the stage 1 acceptance check in plain words


if __name__ == "__main__":  # only run when called as a script, not when imported
    main()  # start
