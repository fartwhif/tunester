"""LLM client for song discovery and the QC gate.

Conventions follow MemeSeek (projects/MemeSeek/llm_client.py):
- OPENAI_BASE_URL includes /v1; endpoint is {base}/chat/completions
- OPENAI_API_KEY bearer auth
- Qwen3 thinking controlled via the body "thinking" param with a small
  budget, so the answer lands in the standard content field
- the answer may still leak into reasoning_content; content is preferred

Backends are tried in priority order (q38fa on llm2, then llm1);
first one to return a valid response wins.
"""
import json
import os
import re
import lookups
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

# Endpoints are deployment config, so nothing is hardcoded: base URLs and
# model ids come from .env / the environment (see _llm_endpoints). An unset
# base URL drops that backend entirely.
DEFAULT_BASE_URL = ""
DEFAULT_MODEL = ""
THINK_BUDGET = int(os.getenv("TUNESTER_LLM_THINK_BUDGET", "100"))
CHAT_TIMEOUT = int(os.getenv("TUNESTER_LLM_TIMEOUT", "240"))
LOAD_WAIT = int(os.getenv("TUNESTER_LLM_LOAD_WAIT", "360"))
PRIMARY_BASE_URL = ""
PRIMARY_MODEL = ""
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


def _chat_key(base_url: str, model: str, max_tokens: int,
              system: str, user: str) -> str:
    """Store key of one LLM answer: sha1 over everything that decides the
    reply. `temperature 0` and no streaming make the reply a function of
    these five values alone; the api key is deliberately absent (it
    authorizes a request, it does not change the answer, and rotating it
    must not invalidate the verdicts it bought)."""
    return lookups.key(base_url, model, str(int(max_tokens)), system, user)


def _llm_endpoints() -> list[LLMEndpoint]:
    """LLM backends in priority order; index 0 is tried first. A backend
    whose base URL is not configured (env / .env) is dropped, so a machine
    with only one backend up declares only that one."""
    eps = [
        LLMEndpoint(
            name="primary",
            base_url=_env("TUNESTER_LLM_PRIMARY_BASE_URL", PRIMARY_BASE_URL),
            api_key=_env("TUNESTER_LLM_PRIMARY_API_KEY")
            or _env("OPENAI_API_KEY"),
            model=_env("TUNESTER_LLM_PRIMARY_MODEL", PRIMARY_MODEL),
        ),
        LLMEndpoint(
            name="fallback",
            base_url=_env("OPENAI_BASE_URL", DEFAULT_BASE_URL),
            api_key=_env("OPENAI_API_KEY"),
            model=_env("TUNESTER_LLM_MODEL", DEFAULT_MODEL),
        ),
    ]
    return [e for e in eps if e.base_url]


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
        # A verdict is worth keeping: `temperature 0` plus a fixed prompt
        # makes this call a pure function of (server, model id, sampling
        # params, prompt), and the QC gate asks the same question about the
        # same candidate every time a library re-tunes it. The store keeps
        # the TEXT, not the HTTP envelope, and only a valid reply is ever
        # published - a timeout, a 503 cold start or an unparseable answer
        # costs the next call the same wait this one paid.
        #
        # The server's base URL belongs in the identity, not only the model
        # id: an inference host answers a model id it does not serve by
        # aliasing it to whatever it has loaded - this box's GPU peer does
        # exactly that - so the same id on another host is a different model.
        # The key is re-derived after a 404 model substitution, so an entry never
        # claims to be an answer from a model that never produced it.
        url = ep.base_url + "/chat/completions"
        deadline = time.time() + budget
        while True:
            ckey = _chat_key(ep.base_url, body["model"], max_tokens,
                             system, user)
            cached = lookups.get(lookups.LLM_STORE, ckey,
                                 lookups.lookup_ttl("llm"))
            if cached is not None:
                if self.log:
                    self.log("        lookup cache hit (llm)")
                return cached.decode("utf-8", "replace")
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
                    # Persisted on the endpoint, not just swapped into this
                    # body: every call re-derives its cache key from
                    # `body["model"]`, and a key built from an id the server
                    # rejected can NEVER hit the verdict the resolved id
                    # produced - each new chat() would re-pay the 404 plus
                    # the /models round-trip for nothing, and the store
                    # would fill under a key nobody asks with. The endpoint
                    # lives as long as the client, so one client resolves
                    # once; a fresh client re-derives once too.
                    ep.model = body["model"] = self._resolve_model(ep)
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
        # Published only here: everything above this line is a failure that
        # must stay unpublished, or one bad moment decides every later run.
        lookups.put(lookups.LLM_STORE, ckey, "llm", ep.base_url,
                    text.encode("utf-8"), lookups.lookup_ttl("llm"),
                    self.log)
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
