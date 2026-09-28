"""LLM client for song discovery and the QC gate.

Conventions follow MemeSeek (projects/MemeSeek/llm_client.py):
- OPENAI_BASE_URL includes /v1; endpoint is {base}/chat/completions
- OPENAI_API_KEY bearer auth
- Qwen3 thinking controlled via the body "thinking" param with a small
  budget, so the answer lands in the standard content field
- the answer may still leak into reasoning_content; content is preferred

Backends are tried in priority order (qwen38-27b, then llm1); the
first one to return a valid response wins.
"""
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://llm1.llmhost.example.com/v1"
DEFAULT_MODEL = "llm1"
THINK_BUDGET = int(os.getenv("TUNESTER_LLM_THINK_BUDGET", "100"))
CHAT_TIMEOUT = int(os.getenv("TUNESTER_LLM_TIMEOUT", "240"))
LOAD_WAIT = int(os.getenv("TUNESTER_LLM_LOAD_WAIT", "360"))
PRIMARY_BASE_URL = "https://qwen38-27b.llmhost.example.com/v1"
PRIMARY_MODEL = "/models/Qwen3.8-27B-UD-Q6_K_XL.gguf"
# max seconds on a non-final backend before failing over to the next one
FAILOVER_BUDGET = int(os.getenv("TUNESTER_LLM_FAILOVER_WAIT", "90"))
# connection errors that mean the endpoint is down, not just slow
_DEAD_REASONS = (ConnectionRefusedError, ConnectionResetError,
                 socket.gaierror)


def _load_env_file() -> dict:
    """KEY=VALUE pairs from the .env next to this file (native dev runs;
    the container entrypoint sources the same file into the real env)."""
    p = Path(__file__).resolve().parent / ".env"
    vals: dict[str, str] = {}
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            vals[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return vals


_ENV_FILE = _load_env_file()


class LLMError(RuntimeError):
    """LLM unreachable, HTTP error, or unparseable reply."""


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply.

    Tolerates prose around the object and a leaked think...end block.
    """
    candidates = [text]
    m = re.search(r"(?s)think(.*?)end", text)
    if m:
        candidates.append(text[: m.start()] + text[m.end():])
    for cand in candidates:
        jm = re.search(r"\{.*\}", cand, re.S)
        if jm:
            try:
                return json.loads(jm.group(0))
            except json.JSONDecodeError:
                continue
    raise LLMError(f"no JSON object in LLM reply: {text[:200]!r}")

def _env(name: str, default: str = "") -> str:
    """Env var first, then .env next to this file, then the default."""
    return os.getenv(name) or _ENV_FILE.get(name, default)


class LLMEndpoint:
    """One LLM backend. resolved_model is set once if the configured
    model name 404s and the server's real model id is substituted."""
    def __init__(self, name: str, base_url: str, api_key: str,
                 model: str):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.resolved_model = False


def _llm_endpoints() -> list[LLMEndpoint]:
    """LLM backends in priority order; index 0 is tried first."""
    return [
        LLMEndpoint(
            name="qwen38-27b",
            base_url=_env("TUNESTER_LLM_PRIMARY_BASE_URL", PRIMARY_BASE_URL),
            api_key=_env("TUNESTER_LLM_PRIMARY_API_KEY")
            or _env("OPENAI_API_KEY"),
            model=_env("TUNESTER_LLM_PRIMARY_MODEL", PRIMARY_MODEL),
        ),
        LLMEndpoint(
            name="llm1",
            base_url=_env("OPENAI_BASE_URL", DEFAULT_BASE_URL),
            api_key=_env("OPENAI_API_KEY"),
            model=_env("TUNESTER_LLM_MODEL", DEFAULT_MODEL),
        ),
    ]


class LLM:
    """Tries the configured backends in priority order per call and
    returns the first valid response."""

    def __init__(self, log=None, endpoints=None):
        self.endpoints = endpoints if endpoints is not None \
            else _llm_endpoints()
        self.log = log

    def chat(self, system: str, user: str, max_tokens: int = 1500) -> str:
        if self.log:
            self.log("llm prompt (system):\n" + system
                     + "\nllm prompt (user):\n" + user)
        errors: list[str] = []
        for i, ep in enumerate(self.endpoints):
            budget = LOAD_WAIT if i == len(self.endpoints) - 1 \
                else FAILOVER_BUDGET
            try:
                if self.log:
                    self.log(f"llm: trying {ep.name}")
                return self._chat_with(ep, system, user, max_tokens, budget)
            except LLMError as e:
                errors.append(f"{ep.name}: {e}")
                if self.log and i < len(self.endpoints) - 1:
                    self.log(f"llm: {ep.name} failed ({e}); trying next")
        raise LLMError("all LLMs failed: " + " | ".join(errors))

    def _chat_with(self, ep: LLMEndpoint, system: str, user: str,
                   max_tokens: int, budget: float) -> str:
        body = {
            "model": ep.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "stream": False,
            "thinking": {"type": "enabled", "budget_tokens": THINK_BUDGET},
        }
        url = ep.base_url + "/chat/completions"
        deadline = time.time() + budget
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise LLMError(f"no response within {budget:.0f}s")
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + ep.api_key,
                },
            )
            try:
                with urllib.request.urlopen(
                        req, timeout=min(CHAT_TIMEOUT, remaining)) as resp:
                    data = json.load(resp)
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:300]
                # 503 "Loading model" — cold start; wait it out
                if e.code == 503 and "load" in detail.lower():
                    time.sleep(5)
                    continue
                # wrong model name — fall back to the server's real id
                if e.code == 404 and "model" in detail.lower() \
                        and not ep.resolved_model:
                    ep.resolved_model = True
                    body["model"] = self._resolve_model(ep)
                    continue
                raise LLMError(f"HTTP {e.code}: {detail}")
            except urllib.error.URLError as e:
                # endpoint down (refused/DNS/reset): fail over at once
                if isinstance(e.reason, _DEAD_REASONS):
                    raise LLMError(f"unreachable: {e.reason}")
                time.sleep(5)
            except Exception:  # read timeout, reset mid-request
                time.sleep(5)
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        text = (msg.get("content") or "").strip()
        if not text:
            text = (msg.get("reasoning_content") or "")[-600:]
        if not text:
            raise LLMError("returned no content")
        return text

    def chat_json(self, system: str, user: str, max_tokens: int = 1500) \
            -> dict:
        return extract_json(self.chat(system, user, max_tokens))

    def _resolve_model(self, ep: LLMEndpoint) -> str:
        req = urllib.request.Request(
            ep.base_url + "/models",
            headers={"Authorization": "Bearer " + ep.api_key},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        models = data.get("data") or data.get("models") or []
        if not models:
            raise LLMError(f"{ep.name} lists no models")
        return models[0].get("id") or models[0].get("name")
