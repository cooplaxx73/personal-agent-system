"""Central LLM router for worker tasks (email summaries, deadline extraction).

Order of preference:
1. the local LLM gateway (:8003) -- gives worker tasks the same provider
   fallback the chat agent gets (groq -> gemini -> cerebras -> openrouter), so a
   single provider's quota can no longer break email summarising;
2. Gemini direct -- backstop for when the gateway service itself is down;
3. Ollama on the PC's GPU -- used when there is no cloud key at all.

Tiers are coded RULES, not an LLM deciding, so choosing a model costs zero
tokens (that's the whole point -- an LLM picking the model would defeat it):

- "fast"  -> qwen2.5:14b      quick, high-context, low-reasoning work:
                              summaries, email categorizing, deadline extraction
- "heavy" -> qwen3-coder:30b  deeper reasoning / code -- reserved for tasks that
                              genuinely need it

Callers choose explicitly:  generate(prompt, tier="fast")
"""
import os
import shutil
import subprocess
import time

import requests

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_VERSION_URL = "http://localhost:11434/api/version"

TIERS = {
    "fast": "qwen2.5:14b",
    "heavy": "qwen3-coder:30b",
}
DEFAULT_TIER = "fast"

# When GEMINI_API_KEY is set (i.e. running on the cloud VM), route all LLM work to
# Gemini instead of the local GPU. On the PC (no key) it stays on Ollama. So the
# same code runs local-and-free on the PC, or cloud-and-free on the VM.
GEMINI_MODEL = "gemini-flash-latest"

# The gateway is OpenAI-compatible and picks the provider itself, so the model
# name here is a placeholder it ignores.
GATEWAY_URL = os.environ.get("LLM_GATEWAY_URL", "http://127.0.0.1:8003/v1/chat/completions")


def _gateway_generate(prompt: str, timeout: int) -> str:
    resp = requests.post(GATEWAY_URL, json={
        "model": "auto",
        "messages": [{"role": "user", "content": prompt}],
    }, timeout=timeout)
    resp.raise_for_status()
    return (resp.json()["choices"][0]["message"].get("content") or "").strip()


def _gateway_up() -> bool:
    try:
        base = GATEWAY_URL.rsplit("/v1/", 1)[0]
        return requests.get(f"{base}/health", timeout=3).ok
    except requests.RequestException:
        return False


def _gemini_generate(prompt: str, timeout: int) -> str:
    key = os.environ["GEMINI_API_KEY"]
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={key}")
    resp = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]},
                         timeout=timeout)
    resp.raise_for_status()
    parts = resp.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip()


def ensure_ollama_running(timeout_seconds: int = 30) -> bool:
    """If the local Ollama server isn't up, try to start it, then wait for it.
    Returns True once reachable. Ollama runs on the host, so the API can launch
    it even though n8n itself lives inside Docker."""
    try:
        requests.get(OLLAMA_VERSION_URL, timeout=3)
        return True
    except requests.RequestException:
        pass

    ollama_exe = shutil.which("ollama")
    if not ollama_exe:
        return False
    try:
        subprocess.Popen([ollama_exe, "serve"],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return False

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            requests.get(OLLAMA_VERSION_URL, timeout=3)
            return True
        except requests.RequestException:
            time.sleep(2)
    return False


def model_for(tier: str) -> str:
    return TIERS.get(tier, TIERS[DEFAULT_TIER])


def generate(prompt: str, tier: str = DEFAULT_TIER, timeout: int = 180) -> str:
    """Run a prompt and return the response text. Prefers the fallback gateway,
    then Gemini direct, then the local Ollama model for the given tier."""
    if _gateway_up():
        try:
            return _gateway_generate(prompt, timeout)
        except requests.RequestException:
            pass          # gateway wobbled -- fall through to Gemini direct
    if os.environ.get("GEMINI_API_KEY"):
        return _gemini_generate(prompt, timeout)
    if not ensure_ollama_running():
        raise RuntimeError("Ollama (local AI) isn't available and couldn't be started")
    resp = requests.post(OLLAMA_URL, json={
        "model": model_for(tier),
        "prompt": prompt,
        "stream": False,
    }, timeout=timeout)
    resp.raise_for_status()
    return resp.json()["response"].strip()


def strip_code_fence(text: str) -> str:
    """Strip a leading ```json / ``` fence some models wrap JSON in."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


if __name__ == "__main__":
    print("Ollama reachable:", ensure_ollama_running())
    for t in TIERS:
        print(f"  tier '{t}' -> {model_for(t)}")
    print("fast test:", repr(generate("Reply with exactly: OK", tier="fast")[:20]))
