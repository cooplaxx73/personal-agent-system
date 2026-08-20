"""OpenAI-compatible LLM gateway with provider fallback.

n8n points its OpenAI Chat Model node at this instead of a single provider. We
try each provider in order and, when one refuses, park it on a cooldown and drop
to the next -- then cycle back to the top once the cooldown expires. That way a
free-tier 429 degrades to "slightly different model" instead of "bot is down",
which is exactly what happened on 2026-07-23 when Gemini's quota ran out and
every message errored.

Why a shim rather than n8n config: the AI Agent node takes exactly ONE language
model connection, so multi-provider fallback can't be expressed in the workflow
without duplicating the whole tool set per provider.

All providers speak the OpenAI wire format, so tool-calling passes straight
through untouched -- verified per provider before this was written. The caller's
`model` field is ignored; each provider gets the model name IT knows.

Ordering: free and fastest first, paid last.
  groq      - free, ~0.3s, generous limits          -> primary
  gemini    - free, ~1.0s                           -> first fallback
  openrouter- PAID credits, only when all else fails-> last resort

cerebras was removed 2026-07-28: HTTP 402 (billing never enabled) meant it never
served a request, and its 1-hour "unavailable" cooldown burned a wasted attempt
on the way down the chain. CEREBRAS_API_KEY is still in secrets.env, so
re-adding it is just restoring the PROVIDERS entry.
"""
import json
import os
import time
from pathlib import Path

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

SECRETS = Path("/home/ubuntu/personal-agent/secrets.env")
LOG = Path("/home/ubuntu/personal-agent/llm_gateway.log")
UPSTREAM_TIMEOUT = 120

# cooldowns: a rate limit is temporary, a billing/auth refusal is not, so they
# get very different penalties -- otherwise we'd retry a dead provider forever
COOLDOWN_RATE_LIMIT = 60
COOLDOWN_UNAVAILABLE = 3600
COOLDOWN_SERVER_ERROR = 30

PROVIDERS = [
    {"name": "groq", "env": "GROQ_API_KEY",
     "base": "https://api.groq.com/openai/v1",
     # llama-3.3-70b-versatile was decommissioned by Groq -- every request 404'd
     # with model_not_found from ~Aug 2026, silently costing us the fast free rung
     # (the fall-through kept things working, which is why nobody noticed).
     # Check `GET /v1/models` on the key before changing this again.
     "model": "openai/gpt-oss-120b"},
    {"name": "gemini", "env": "GEMINI_API_KEY",
     "base": "https://generativelanguage.googleapis.com/v1beta/openai",
     "model": "gemini-flash-latest"},
    {"name": "openrouter", "env": "OPENROUTER_API_KEY",
     "base": "https://openrouter.ai/api/v1",
     # gpt-4o-mini, not llama-3.3-70b: llama scored 14/18 on the real tool suite
     # and NEVER honoured "ask before the first search" (0/3), which no prompt
     # wording fixed. gpt-4o-mini scored 18/18. Costs ~$0.0002 more per
     # conversation on a rung that only runs when groq AND gemini are exhausted.
     "model": "openai/gpt-4o-mini"},
]

_cooldown: dict[str, float] = {}
_stats: dict[str, dict] = {p["name"]: {"ok": 0, "fail": 0, "last_error": ""} for p in PROVIDERS}

app = FastAPI()


def _secrets() -> dict:
    """Read on demand so a rotated key takes effect without a restart."""
    out = {}
    try:
        for line in SECRETS.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.strip().partition("=")
                out[k] = v
    except OSError:
        pass
    return out


def _log(msg: str):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def _available(name: str) -> bool:
    return time.time() >= _cooldown.get(name, 0)


def _park(name: str, seconds: int, why: str):
    _cooldown[name] = time.time() + seconds
    _stats[name]["fail"] += 1
    _stats[name]["last_error"] = why[:200]
    _log(f"PARK {name} for {seconds}s -- {why[:160]}")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/status")
def status():
    now = time.time()
    return {"providers": [
        {"name": p["name"], "model": p["model"],
         "available": _available(p["name"]),
         "cooldown_s": max(0, round(_cooldown.get(p["name"], 0) - now)),
         **_stats[p["name"]]}
        for p in PROVIDERS]}


@app.get("/v1/models")
def models():
    """n8n wants a model list; expose one virtual model since we pick per-call."""
    return {"object": "list", "data": [
        {"id": "auto", "object": "model", "owned_by": "gateway"}]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    secrets = _secrets()
    # streaming is forced off: the agent needs the whole message (tool calls
    # included) and mixing SSE into the fallback logic buys nothing here
    body.pop("stream", None)

    errors = []
    for p in PROVIDERS:
        name = p["name"]
        key = secrets.get(p["env"], "")
        if not key:
            errors.append(f"{name}: no key")
            continue
        if not _available(name):
            errors.append(f"{name}: cooling down")
            continue

        payload = dict(body)
        payload["model"] = p["model"]      # each provider names models its own way

        r = None
        for attempt in (1, 2):             # attempt 2 only happens on an empty reply
            try:
                r = requests.post(f"{p['base']}/chat/completions",
                                  headers={"Authorization": f"Bearer {key}",
                                           "Content-Type": "application/json"},
                                  json=payload, timeout=UPSTREAM_TIMEOUT)
            except requests.RequestException as e:
                _park(name, COOLDOWN_SERVER_ERROR, f"{type(e).__name__}: {e}")
                errors.append(f"{name}: {type(e).__name__}")
                r = None
                break
            if r.status_code != 200:
                break
            _m = (r.json().get("choices") or [{}])[0].get("message") or {}
            if (_m.get("content") or "").strip() or _m.get("tool_calls"):
                break                      # got something usable
            if attempt == 1:
                _log(f"EMPTY reply from {name} -- retrying same provider")
        if r is None:
            continue

        if r.status_code == 200:
            body = r.json()
            msg = (body.get("choices") or [{}])[0].get("message") or {}
            if not (msg.get("content") or "").strip() and not msg.get("tool_calls"):
                # 200 but the model said nothing at all -- no text, no tool call.
                # Useless to the agent, so treat it as a failure and try the next
                # provider. No cooldown: the provider is healthy, that roll wasn't.
                _log(f"EMPTY reply from {name} (falling through)")
                _stats[name]["fail"] += 1
                _stats[name]["last_error"] = "empty response (no content, no tool_calls)"
                errors.append(f"{name}: empty")
                continue
            _stats[name]["ok"] += 1
            _log(f"OK {name} ({p['model']})")
            out = JSONResponse(content=body)
            out.headers["X-LLM-Provider"] = name
            out.headers["X-LLM-Model"] = p["model"]
            return out

        detail = r.text[:200].replace("\n", " ")
        if r.status_code == 429:
            _park(name, COOLDOWN_RATE_LIMIT, f"429 {detail}")
        elif r.status_code in (401, 402, 403):
            _park(name, COOLDOWN_UNAVAILABLE, f"{r.status_code} {detail}")
        elif r.status_code >= 500:
            _park(name, COOLDOWN_SERVER_ERROR, f"{r.status_code} {detail}")
        else:
            # 400-class. Usually this is the provider rejecting the MODEL's own
            # tool call (Groq validates them server-side and llama occasionally
            # emits a malformed one when many tools are in play). That is
            # roll-specific, so another provider commonly succeeds on the very
            # same request -- fall through rather than failing the user. No
            # cooldown: the provider itself is healthy.
            _log(f"BAD REQUEST via {name} (falling through): {detail}")
            _stats[name]["fail"] += 1
            _stats[name]["last_error"] = f"{r.status_code} {detail}"[:200]
        errors.append(f"{name}: {r.status_code}")

    _log("ALL PROVIDERS FAILED -- " + "; ".join(errors))
    return JSONResponse(status_code=503, content={"error": {
        "message": "All LLM providers are unavailable right now: " + "; ".join(errors),
        "type": "all_providers_failed"}})
