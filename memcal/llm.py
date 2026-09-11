"""Completion clients for OpenRouter, Claude Code, Codex, and Antigravity.

Stdlib only. The dream pass sends N independent calls that share a byte-identical
prefix, so the prefix is marked cacheable and the varying bundle goes last.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

BASE_URL = "https://openrouter.ai/api/v1"
RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}

#: Fallback phrases for error bodies that omit a numeric status. Numeric `code` wins;
#: successful bodies with `choices` never enter this path.
_RATE_LIMIT_WORDS = re.compile(r"rate.?limit|too many requests|temporarily unavailable",
                               re.I)


def _error_status(raw: dict) -> int | None:
    """Return an error status embedded in a nominally successful response."""
    if not isinstance(raw, dict) or raw.get("choices"):
        return None
    error = raw.get("error")
    if not error:
        return None
    if isinstance(error, dict):
        code = error.get("code") or error.get("status")
        if isinstance(code, int):
            return code
        if isinstance(code, str) and code.isdigit():
            return int(code)
        if _RATE_LIMIT_WORDS.search(str(error.get("message") or "")):
            return 429
        return 500
    return 429 if _RATE_LIMIT_WORDS.search(str(error)) else 500

#: Capacity refusals use a wall-clock retry budget. Flex never falls back to standard
#: service, so a shortage must remain retryable without changing the requested tier.
CAPACITY_STATUS = {429, 503}
#: Total wall-clock a single request may spend waiting out capacity, before giving up.
CAPACITY_BUDGET = 1800.0
#: Longest single sleep. A provider's own `Retry-After` is believed above this.
MAX_BACKOFF = 300.0

# $/MTok, so a pass can be priced before it is spent. Sonnet 5 is on introductory
# pricing through 2026-08-31; its standard rate is 3.00/15.00.
# The open-weight models are priced per one specific provider endpoint, because rates
# on OpenRouter vary several-fold across providers for the same model — glm-5.2 alone
# spans 0.74 to 3.03 in. Each rate below is the endpoint the config actually pins.
PRICES: dict[str, tuple[float, float]] = {
    "anthropic/claude-sonnet-5": (2.00, 10.00),
    "anthropic/claude-opus-5": (5.00, 25.00),
    "anthropic/claude-haiku-4-5": (1.00, 5.00),
    "deepseek/deepseek-v4-flash": (0.14, 0.28),   # novita, fp8
    "z-ai/glm-5.2": (0.74, 2.32),                 # novita, fp8
    "stepfun/step-3.7-flash": (0.20, 1.15),       # deepinfra
    "x-ai/grok-4.5": (2.00, 6.00),                # xai (first-party, only provider)
    # openai, standard tier. Cheaper than step-3.7-flash in both directions, which is the
    # whole reason to look at it. `FLEX_PRICES` below is what it actually bills at, since
    # its endpoint asks for the flex tier. Above 272k prompt tokens the rate doubles to
    # 0.20/0.90; memcal's largest observed request was 40k, so that tier is noted and
    # not modelled.
    "openai/gpt-5.6-luna": (0.10, 0.60),          # openai (first-party)
    # The capable end of the same family. Both pin `openai` and both ask for flex, so
    # what they actually bill is FLEX_PRICES below; these are the standard rates, kept
    # so a run with the tier unavailable still prices instead of going silent.
    "openai/gpt-5.6-terra": (1.00, 6.00),         # openai (first-party)
    "openai/gpt-5.6-sol": (5.00, 30.00),          # openai (first-party)
}

#: Half price, for the endpoints that ask for `service_tier: flex`. Kept as its own
#: table rather than a 0.5 multiplier because "flex is half" is a current fact about two
#: providers, not a permanent pricing rule — and a multiplier would misprice the first
#: provider that discounts input and output differently.
FLEX_PRICES: dict[str, tuple[float, float]] = {
    "openai/gpt-5.6-luna": (0.05, 0.30),
    "openai/gpt-5.6-terra": (0.50, 3.00),
    "openai/gpt-5.6-sol": (2.50, 15.00),
}
#: Read timeout for a flex request. The tier is explicitly slower and its own docs
#: recommend 15 minutes; the default 300s turns a slow success into a paid retry.
FLEX_TIMEOUT = 900.0
CACHE_WRITE = 1.25   # 5-minute ttl; the 1h ttl costs 2.0 and needs 3+ reads to pay off
CACHE_READ = 0.10
# Below this many tokens a prefix silently will not cache — no error, no entry.
CACHE_MIN: dict[str, int] = {
    "anthropic/claude-opus-5": 512,
    "anthropic/claude-sonnet-5": 1024,
    "anthropic/claude-haiku-4-5": 4096,
}
# Models whose pinned endpoint ignores explicit prompt-cache markers.
NO_PROMPT_CACHE = {
    "deepseek/deepseek-v4-flash",
    "z-ai/glm-5.2",
    "stepfun/step-3.7-flash",
    "x-ai/grok-4.5",
}
# OpenAI caches automatically and does not need to appear in this set.


@dataclass(frozen=True)
class Endpoint:
    """Provider routing, output format, reasoning budget, and service tier."""
    provider: tuple[str, ...] = ()
    json_mode: str = "schema"
    reasoning_effort: str | None = None
    ceiling_boost: float = 1.0
    think_tokens: int = 0
    #: OpenRouter service tier; flex trades latency/capacity for lower cost.
    service_tier: str | None = None


ENDPOINTS: dict[str, Endpoint] = {
    # Pin providers because structured-output support differs between deployments.
    "stepfun/step-3.7-flash": Endpoint(("stepfun",), "schema", "medium", 2.2, 4_000),
    "z-ai/glm-5.2": Endpoint(("novita",), "prompt", "low", 2.2, 4_000),
    "deepseek/deepseek-v4-flash": Endpoint(("novita",), "object", "medium", 2.2, 4_000),
    "x-ai/grok-4.5": Endpoint(("xai",), "schema", "medium", 2.2, 4_000),
    # Reasoning floors are measured API tokens per bundle, not visible summary text.
    "openai/gpt-5.6-luna": Endpoint(("openai",), "schema", "medium", 1.0, 1_500,
                                    service_tier="flex"),
    "openai/gpt-5.6-terra": Endpoint(("openai",), "schema", "medium", 1.0, 6_000,
                                     service_tier="flex"),
    "openai/gpt-5.6-sol": Endpoint(("openai",), "schema", "high", 1.0, 8_000,
                                   service_tier="flex"),
}


def endpoint(model: str) -> Endpoint:
    return ENDPOINTS.get(model, Endpoint())


def rates(model: str) -> tuple[float, float] | None:
    """Return the endpoint's effective input/output price per million tokens."""
    if endpoint(model).service_tier == "flex" and model in FLEX_PRICES:
        return FLEX_PRICES[model]
    return PRICES.get(model)


def price(model: str, tokens: int, *, output: bool = False) -> float:
    rate = rates(model)
    if not rate:
        return 0.0
    return tokens * rate[1 if output else 0] / 1_000_000


def packed_cost(model: str, *, prefix_tokens: int, suffix_tokens: int,
                output_tokens: int, requests: int, max_parallel: int) -> dict:
    """Price one packed propose wave using the endpoint's real cache behavior."""
    if not requests or not rates(model):
        return {"priced": False, "model": model}
    cache = model not in NO_PROMPT_CACHE
    if cache:
        misses = min(requests, max_parallel)
        prefix_now = price(model, int(prefix_tokens * misses * CACHE_WRITE)) \
            + price(model, int(prefix_tokens * (requests - misses) * CACHE_READ))
        prefix_warmed = price(model, int(prefix_tokens * CACHE_WRITE)) \
            + price(model, int(prefix_tokens * (requests - 1) * CACHE_READ))
    else:
        misses = requests
        prefix_now = prefix_warmed = price(model, prefix_tokens * requests)
    return {
        "priced": True, "model": model, "cache": cache,
        "input": round(price(model, suffix_tokens) + prefix_now, 4),
        "prefix_now": round(prefix_now, 4),
        "prefix_warmed": round(prefix_warmed, 4),
        "output_ceiling": round(price(model, output_tokens, output=True), 4),
        "cache_misses": misses,
    }


#: Process-wide lock for usage accounting across parallel clients.
_LEDGER = threading.Lock()


class QuotaExhausted(RuntimeError):
    """The account is out of allowance, and no amount of retrying changes that.

    Distinct from `LLMError` because the recovery is different in kind: a failed request
    is worth splitting and re-sending, and a spent subscription is worth stopping for. A
    pass that retries into a quota wall spends its whole budget of wall-clock producing
    the same sentence eight times in parallel, and then reports a low score for a run
    where no model read anything.
    """


class LLMError(RuntimeError):
    pass


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    #: Included in completion_tokens; sourced from API usage, not visible reasoning.
    reasoning_tokens: int = 0
    cost: float = 0.0
    #: Logical completions that returned a Reply.
    calls: int = 0
    #: HTTP attempts, including retries and failed requests.
    requests: int = 0
    #: Logical completions that raised before producing a Reply.
    failed: int = 0
    #: Backoff thread-seconds, summed across parallel calls.
    waited: float = 0.0

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.cached_tokens += other.cached_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.cost += other.cost
        self.calls += other.calls
        self.requests += other.requests
        self.failed += other.failed
        self.waited += other.waited

    def summary(self) -> str:
        """Summarize tokens, cost, retries, failures, and backoff."""
        # A subscription-authenticated CLI backend reports no cost at all, so "$0.0000"
        # over a full pass reads as a free run rather than an unpriced one — the same
        # false zero the `requests`/`failed_calls` columns exist to stop.
        priced = f"${self.cost:.4f}" if self.cost or not self.calls else "cost not reported"
        line = (f"{self.calls} calls · {self.prompt_tokens} in "
                f"({self.cached_tokens} cached) · {self.completion_tokens} out "
                f"· {priced}")
        tail = []
        if self.requests != self.calls:
            tail.append(f"{self.requests} requests")
        if self.failed:
            tail.append(f"{self.failed} failed")
        if self.waited >= 1:
            tail.append(f"{self.waited:.0f}s in backoff")
        return line + (" · " + ", ".join(tail) if tail else "")


@dataclass
class Tally:
    """Per-call request, retry, and wait counters that survive exceptions."""
    requests: int = 0
    faults: int = 0
    waits: int = 0
    waited: float = 0.0


@dataclass
class Reply:
    text: str
    data: dict | list | None
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    reasoning: str = ""
    generation_id: str = ""
    #: Provider stop reason; "length" means the output was truncated.
    finish_reason: str = ""
    #: Capacity tier actually served by the provider.
    service_tier: str = ""
    #: HTTP attempts and backoff time used to produce this reply.
    requests: int = 1
    waited: float = 0.0

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


class CompletionClient:
    """The small client surface used by propose, Merge, sweep, and live writes."""

    def __init__(self) -> None:
        self.usage = Usage()

    def _charge(self, spent: Usage) -> None:
        with _LEDGER:
            self.usage.add(spent)

    def map(self, jobs: Iterable, worker: Callable, max_parallel: int = 8,
            on_done: Callable[[int, object], None] | None = None) -> list:
        jobs = list(jobs)
        if not jobs:
            return []
        if max_parallel <= 1 or len(jobs) == 1:
            out = []
            for index, job in enumerate(jobs):
                value = _safe(worker, job)
                out.append(value)
                if on_done:
                    on_done(index, value)
            return out
        with ThreadPoolExecutor(max_workers=min(max_parallel, len(jobs))) as pool:
            futures = {pool.submit(_safe, worker, job): index
                       for index, job in enumerate(jobs)}
            out = [None] * len(jobs)
            for future in as_completed(futures):
                index = futures[future]
                value = future.result()
                out[index] = value
                if on_done:
                    on_done(index, value)
            return out


class OpenRouter(CompletionClient):
    def __init__(self, api_key: str | None, *, timeout: float = 300.0,
                 referer: str = "https://github.com/example/memcal", title: str = "memcal",
                 on_retry: Callable[[str], None] | None = None):
        if not api_key:
            raise LLMError(
                "no OpenRouter API key. Put it in memcal/.env (a bare sk-or-... line works) "
                "or export OPENROUTER_API_KEY."
            )
        super().__init__()
        self.api_key = api_key
        self.timeout = timeout
        #: Called before any wait long enough to look like a hang. A run that waits out
        #: a busy hour in silence is indistinguishable from one that has died.
        self.on_retry = on_retry
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": referer,
            "X-Title": title,
        }
    # ------------------------------------------------------------------ http --
    def _post(self, path: str, payload: dict, *, attempts: int = 6,
              timeout: float | None = None,
              capacity_budget: float = CAPACITY_BUDGET,
              tally: Tally | None = None) -> dict:
        """POST with jittered retries and a separate wall-clock capacity budget."""
        body = json.dumps(payload).encode("utf-8")
        last: Exception | None = None
        started = time.monotonic()
        tally = tally if tally is not None else Tally()
        while True:
            req = urllib.request.Request(f"{BASE_URL}{path}", data=body, headers=self.headers)
            capacity = False
            wait = min(MAX_BACKOFF, 2.0 ** min(tally.faults + tally.waits, 10))
            # Charged before the attempt, not after it: a request that raises is a
            # request the provider saw and the clock felt.
            tally.requests += 1
            self._charge(Usage(requests=1))
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
                # OpenRouter can return provider errors inside an HTTP 200 body.
                status = _error_status(raw)
                if status is None:
                    return raw
                last = LLMError(f"in-body {status}: {str(raw.get('error'))[:300]}")
                if status not in RETRY_STATUS:
                    raise last
                capacity = status in CAPACITY_STATUS
            except urllib.error.HTTPError as exc:
                code = exc.code
                headers = exc.headers
                try:
                    detail = exc.read().decode("utf-8", "replace")[:400]
                finally:
                    exc.close()
                last = LLMError(f"HTTP {code}: {detail}")
                if code not in RETRY_STATUS:
                    raise last from exc
                capacity = code in CAPACITY_STATUS
                retry_after = (headers or {}).get("Retry-After")
                if retry_after:
                    try:
                        wait = min(MAX_BACKOFF, float(retry_after))
                    except ValueError:
                        pass
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last = LLMError(f"{type(exc).__name__}: {exc}")

            spent = time.monotonic() - started
            if capacity:
                tally.waits += 1
                if spent + wait > capacity_budget:
                    raise LLMError(
                        f"gave up after {spent:.0f}s of capacity waits "
                        f"({tally.waits} of them, {tally.requests} requests): "
                        f"{last}") from last
            else:
                tally.faults += 1
                if tally.faults >= attempts:
                    raise last or LLMError("request failed")
            pause = random.uniform(0.0, wait) + 0.25
            if self.on_retry and pause >= 5.0:
                # Surface waits long enough to look like a hung run.
                self.on_retry(f"{'capacity' if capacity else 'error'}; waiting "
                              f"{pause:.0f}s ({spent:.0f}s so far) — {str(last)[:80]}")
            tally.waited += pause
            self._charge(Usage(waited=pause))
            time.sleep(pause)

    # ------------------------------------------------------------- completion --
    def complete(
        self,
        *,
        model: str,
        prefix: str,
        suffix: str,
        schema: dict | None = None,
        schema_name: str = "diff",
        max_tokens: int = 8000,
        cache_prefix: bool = True,
        capture_reasoning: bool = False,
        provider: list[str] | None = None,
        json_object: bool = False,
        reasoning_effort: str | None = None,
        turns: list[dict] | None = None,
        service_tier: str | None = None,
    ) -> Reply:
        """Complete one cached-prefix request using the model's endpoint contract."""
        spec = endpoint(model)
        if provider is None and spec.provider:
            provider = list(spec.provider)
        if reasoning_effort is None:
            reasoning_effort = spec.reasoning_effort
        if schema is None and not json_object:
            json_object = spec.json_mode == "object"
        if schema is not None and spec.json_mode != "schema":
            # The endpoint 400s (or silently misroutes) on a schema it cannot honour.
            # Dropping it here means the shape rides on the prompt instead, which is
            # what build_prefix already arranges for these models.
            schema = None
            json_object = spec.json_mode == "object"
        if cache_prefix and model in NO_PROMPT_CACHE:
            cache_prefix = False
        system_part: dict = {"type": "text", "text": prefix}
        if cache_prefix:
            system_part["cache_control"] = {"type": "ephemeral"}
        payload: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": [system_part]},
                {"role": "user", "content": suffix},
                *(turns or ()),
            ],
            "max_tokens": max_tokens,
            "usage": {"include": True},
        }
        if provider:
            payload["provider"] = {"order": list(provider), "allow_fallbacks": False}
        # Top-level, not under `provider` — OpenRouter reads it as a routing constraint
        # rather than a provider preference. `service_tier=""` from a caller forces the
        # default tier back on for a request that genuinely cannot wait.
        if service_tier is None:
            service_tier = spec.service_tier
        if service_tier:
            payload["service_tier"] = service_tier
        if capture_reasoning or reasoning_effort:
            # Reading what the model actually reasoned is how we find out whether the
            # prompt says what we think it says. Off by default — it costs tokens.
            payload["reasoning"] = {"effort": reasoning_effort or "low"}
        if schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            }
        elif json_object:
            payload["response_format"] = {"type": "json_object"}
        # Flex trades latency for half the rate, so the default 300s read timeout is
        # the wrong one for it: a request that would have been served in eight
        # minutes gets abandoned at five and retried at full price. OpenAI recommends
        # 15 minutes for the tier and that is what this is.
        #: One logical call, however many requests it takes. Everything from here is
        #: wrapped so that a call which never returns a Reply is still *counted*: it has
        #: no generation id, so no `generations` row can exist for it, and the run-level
        #: `failed` count is the only place it can ever be recorded.
        tally = Tally()
        try:
            raw = self._post("/chat/completions", payload,
                             timeout=FLEX_TIMEOUT if service_tier == "flex" else None,
                             tally=tally)
            reply = _reply_from(raw, model, tally)
        except Exception as exc:
            self._charge(Usage(failed=1))
            # The tally rides out on the exception. It is the only way an attempt count
            # survives a raise, and the caller that catches this is the one holding the
            # prompt, the bundles and somewhere to write them — see
            # `propose._record_failure`.
            exc.tally = tally
            raise
        self._charge(reply.usage)
        return reply

    def list_models(self, filter_text: str = "") -> list[dict]:
        req = urllib.request.Request(f"{BASE_URL}/models", headers=self.headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8")).get("data", [])
        if filter_text:
            data = [m for m in data if filter_text.lower() in m["id"].lower()]
        return data


def _programmatic_prompt(prefix: str, suffix: str,
                         turns: list[dict] | None = None) -> str:
    """Flatten messages for stateless agent CLIs."""
    parts = ["<system>\n" + prefix + "\n</system>",
             "<user>\n" + suffix + "\n</user>"]
    for turn in turns or ():
        role = str(turn.get("role") or "user")
        parts.append(f"<{role}>\n{turn.get('content') or ''}\n</{role}>")
    return "\n\n".join(parts)


#: Event and item type names Codex has used for reasoning across CLI versions. The name
#: has already changed once (`agent_reasoning` became an `item.completed` of type
#: `reasoning`), and a miss is silent: no error, just an empty field.
_CODEX_REASONING_TYPES = {"reasoning", "agent_reasoning", "reasoning_summary",
                          "agent_reasoning_section_break"}
#: Where the text sits once the item is found. `summary` is a list of parts on the
#: Responses-shaped item; the others are flat strings.
_CODEX_REASONING_FIELDS = ("text", "content", "summary_text", "delta")


def _codex_reasoning(events: list[dict]) -> str:
    """The readable reasoning summary out of one `codex exec --json` stream.

    Only the summary is ever readable: the chain arrives as `encrypted_content` and
    stays encrypted in the session rollout too. Returns "" when the CLI emits none.
    """
    parts: list[str] = []

    def take(node) -> None:
        if isinstance(node, str):
            if node.strip():
                parts.append(node.strip())
        elif isinstance(node, list):
            for child in node:
                take(child)
        elif isinstance(node, dict):
            for field_name in _CODEX_REASONING_FIELDS:
                if node.get(field_name):
                    take(node[field_name])
                    return

    for event in events:
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        kinds = {str(event.get("type") or ""), str(item.get("type") or "")}
        if not kinds & _CODEX_REASONING_TYPES:
            continue
        source = item or event
        for field_name in ("summary", *_CODEX_REASONING_FIELDS):
            if source.get(field_name):
                take(source[field_name])
                break
    return "\n\n".join(dict.fromkeys(parts)).strip()


#: The vendor prefix `ENDPOINTS` keys on, per programmatic provider. A CLI backend is
#: given the bare native name (`gpt-5.6-luna`), so going *back* to the prefixed key is
#: the only way it can read the same spec the OpenRouter path reads.
#: Antigravity is deliberately absent: it serves Gemini, Claude and open-weight models
#: from one command, so there is no single vendor prefix that would find the right
#: `ENDPOINTS` row. Its model names carry their own reasoning budget instead.
_VENDOR_PREFIX = {"claude-code": "anthropic/", "codex": "openai/"}


def _native_model(provider: str, model: str) -> str:
    prefix = _VENDOR_PREFIX.get(provider, "")
    return model[len(prefix):] if prefix and model.startswith(prefix) else model


#: Fallback rosters for providers whose names cannot be derived from vendor prefixes.
STATIC_PROVIDER_MODELS: dict[str, tuple[str, ...]] = {
    "antigravity": (
        "gemini-3.8-flash-high", "gemini-3.8-flash-medium", "gemini-3.8-flash-low",
        "gemini-3.7-flash-high", "gemini-3.7-flash-medium", "gemini-3.7-flash-low",
        "gemini-3.6-flash-high", "gemini-3.6-flash-medium", "gemini-3.6-flash-low",
        "gemini-3.1-pro-high", "gemini-3.1-pro-low",
        "claude-sonnet-4-6", "claude-opus-4-6-thinking",
        "gpt-oss-120b-medium",
    ),
}


def catalog(provider: str) -> list[tuple[str, str]]:
    """`(what to configure, what to price it as)` for models memcal already knows.

    Named the way that provider names them. A model with no price of its own is paired
    with itself, so `rates()` returns None for it and every surface says "no price on
    file" rather than quoting somebody else's.
    """
    prefix = _VENDOR_PREFIX.get(provider, "")
    known = sorted(set(PRICES) | set(ENDPOINTS))
    if provider == "openrouter":
        return [(model, model) for model in known]
    if prefix:
        return [(model[len(prefix):], model)
                for model in known if model.startswith(prefix)]
    return [(model, model) for model in STATIC_PROVIDER_MODELS.get(provider, ())]


def list_models(cfg, provider: str) -> tuple[list[str], str]:
    """List provider models, using a CLI roster when one is available."""
    provider = str(provider or "").strip().lower()
    known = [native for native, _priced in catalog(provider)]
    if provider != "antigravity":
        return known, "memcal's own table"
    backend = PROVIDER_COMMANDS.get(provider)
    command = backend.command(cfg) if backend else ""
    if not command or not (Path(command).is_file() or shutil.which(command)):
        return known, "memcal's own table — `agy` is not on this PATH to ask"
    try:
        proc = subprocess.run([command, "models"], capture_output=True, text=True,
                              timeout=30, cwd=str(cfg.home))
    except (OSError, subprocess.SubprocessError):
        return known, "memcal's own table — `agy models` did not answer"
    if proc.returncode:
        return known, "memcal's own table — `agy models` failed"
    # `agy models` emits `id<TAB>Human Name`; skip status banners.
    found = []
    for line in (proc.stdout or "").splitlines():
        name = line.split("\t", 1)[0].strip()
        if name and " " not in name and not name.endswith(":"):
            found.append(name)
    return (found, "`agy models`") if found else (
        known, "memcal's own table — `agy models` listed nothing")


def serves(provider: str, model: str) -> bool | None:
    """Return whether a provider roster names a model, or None without a roster."""
    provider = str(provider or "").strip().lower()
    if provider == "openrouter" or provider not in PROVIDER_DEFAULT_MODELS:
        return None
    model = str(model or "").strip()
    if not model:
        return True                        # unset means the provider default, always fine
    known = {native for native, _priced in catalog(provider)}
    return _native_model(provider, model) in known or model in known


def belongs_elsewhere(provider: str, model: str) -> str:
    """Return a different provider known to serve the model, else an empty string."""
    provider = str(provider or "").strip().lower()
    model = str(model or "").strip()
    if not model or not provider or serves(provider, model) is not False:
        return ""
    for other, rows in ((name, catalog(name)) for name in PROVIDER_DEFAULT_MODELS):
        if other in (provider, "openrouter"):
            # OpenRouter's price catalog is not an ownership roster.
            continue
        if any(model == native for native, _priced in rows):
            return other
    return ""


def _spec_for(provider: str, model: str) -> Endpoint:
    """The `ENDPOINTS` row for a model named the way a CLI backend names it.

    `ENDPOINTS` is keyed on the OpenRouter id and the CLI backends are configured with
    the bare native one, so a direct `endpoint()` lookup returns the empty default and
    the tuned reasoning budget does not apply. Re-attaching the vendor prefix makes one
    spec table serve both routes.
    """
    native = _native_model(provider, model)
    return ENDPOINTS.get(_VENDOR_PREFIX.get(provider, "") + native) or endpoint(model)


class ProgrammaticClient(CompletionClient):
    """Common subprocess lifecycle for authenticated agent CLI backends."""

    provider = ""

    def __init__(self, command: str, *, cwd: Path, timeout: float = 900.0):
        super().__init__()
        self.command = command
        self.cwd = Path(cwd)
        self.timeout = timeout

    def _run(self, args: list[str], prompt: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                args, input=prompt, text=True, capture_output=True, cwd=self.cwd,
                timeout=self.timeout, check=False,
            )
        except FileNotFoundError as exc:
            raise LLMError(
                f"{self.provider} command not found: {self.command}. "
                f"Run `memcal setup` after installing it.") from exc
        except subprocess.TimeoutExpired as exc:
            raise LLMError(
                f"{self.provider} timed out after {self.timeout:.0f}s") from exc

    #: Said by a CLI backend that is out of subscription allowance. Matched on the
    #: message because that is all these commands give: the exit code is the same one a
    #: malformed prompt gets.
    _OUT_OF_QUOTA = re.compile(
        r"quota (?:reached|exceeded|exhausted)|out of (?:quota|credits)|"
        r"usage limit reached|upgrade your (?:subscription|plan)", re.I)

    @classmethod
    def _failure(cls, proc: subprocess.CompletedProcess,
                 detail: str = "") -> RuntimeError:
        message = " ".join(str(detail or proc.stderr or proc.stdout
                               or "no output").split())[:600]
        if cls._OUT_OF_QUOTA.search(message):
            return QuotaExhausted(message)
        return LLMError(message)


class ClaudeCode(ProgrammaticClient):
    """Claude Code print mode as a stateless structured-completion backend."""

    provider = "claude-code"

    def complete(self, *, model: str, prefix: str, suffix: str,
                 schema: dict | None = None, schema_name: str = "diff",
                 max_tokens: int = 8000, cache_prefix: bool = True,
                 capture_reasoning: bool = False, provider=None,
                 json_object: bool = False, reasoning_effort: str | None = None,
                 turns: list[dict] | None = None,
                 service_tier: str | None = None) -> Reply:
        del schema_name, max_tokens, cache_prefix, capture_reasoning, provider
        del json_object, service_tier
        if reasoning_effort is None:
            reasoning_effort = _spec_for(self.provider, model).reasoning_effort
        args = [self.command, "-p", "--model", _native_model(self.provider, model),
                "--output-format", "json", "--no-session-persistence",
                "--safe-mode", "--disable-slash-commands", "--tools", ""]
        if schema:
            args += ["--json-schema", json.dumps(schema, separators=(",", ":"))]
        if reasoning_effort:
            args += ["--effort", reasoning_effort]
        proc = self._run(args, _programmatic_prompt(prefix, suffix, turns))
        try:
            raw = json.loads(proc.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise self._failure(proc, f"Claude Code returned invalid JSON: {exc}") from exc
        if proc.returncode or raw.get("is_error"):
            raise self._failure(proc, raw.get("result") or raw.get("terminal_reason") or "")
        text = str(raw.get("result") or "")
        data = raw.get("structured_output")
        if data is None:
            data = _parse_json(text)
        usage_raw = raw.get("usage") or {}
        prompt_tokens = sum(int(usage_raw.get(name) or 0) for name in (
            "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        finish = str(raw.get("stop_reason") or raw.get("terminal_reason") or "")
        if finish == "max_tokens":
            finish = "length"
        spent = Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=int(usage_raw.get("output_tokens") or 0),
            cached_tokens=int(usage_raw.get("cache_read_input_tokens") or 0),
            reasoning_tokens=int((usage_raw.get("output_tokens_details") or {}).get(
                "thinking_tokens") or 0),
            cost=float(raw.get("total_cost_usd") or 0), calls=1, requests=1,
        )
        reply = Reply(
            text=text, data=data, usage=spent,
            model=str(raw.get("model") or _native_model(self.provider, model)),
            generation_id="claude-" + str(raw.get("session_id") or raw.get("uuid") or
                                           time.time_ns()),
            # Present only when the CLI surfaces it; "" otherwise, which `memcal trace`
            # prints nothing for.
            reasoning=str(raw.get("thinking") or raw.get("reasoning") or "").strip(),
            finish_reason=finish, service_tier=str(usage_raw.get("service_tier") or ""),
        )
        self._charge(spent)
        return reply


class Codex(ProgrammaticClient):
    """Codex exec mode as a stateless structured-completion backend."""

    provider = "codex"

    def complete(self, *, model: str, prefix: str, suffix: str,
                 schema: dict | None = None, schema_name: str = "diff",
                 max_tokens: int = 8000, cache_prefix: bool = True,
                 capture_reasoning: bool = False, provider=None,
                 json_object: bool = False, reasoning_effort: str | None = None,
                 turns: list[dict] | None = None,
                 service_tier: str | None = None) -> Reply:
        del schema_name, max_tokens, cache_prefix, capture_reasoning, provider
        del json_object, service_tier
        spec = _spec_for(self.provider, model)
        if reasoning_effort is None:
            reasoning_effort = spec.reasoning_effort
        args = [self.command, "--ask-for-approval", "never", "exec", "--ephemeral",
                "--ignore-user-config", "--skip-git-repo-check", "--sandbox", "read-only",
                "--model", _native_model(self.provider, model), "--json"]
        schema_path = ""
        if schema:
            fd, schema_path = tempfile.mkstemp(prefix="memcal-schema-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(schema, fh, separators=(",", ":"))
            args += ["--output-schema", schema_path]
        if reasoning_effort:
            args += ["--config", f'model_reasoning_effort="{reasoning_effort}"']
        # The raw chain arrives encrypted and the session rollout keeps it that way, so
        # a summary is the only readable reasoning this backend can return — and it is
        # off by default, which is why every trace it wrote stored an empty field.
        args += ["--config", 'model_reasoning_summary="detailed"',
                 "--config", "show_raw_agent_reasoning=true"]
        args.append("-")
        try:
            proc = self._run(args, _programmatic_prompt(prefix, suffix, turns))
        finally:
            if schema_path:
                try:
                    os.unlink(schema_path)
                except OSError:
                    pass
        events = []
        try:
            events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise self._failure(proc, f"Codex returned invalid JSONL: {exc}") from exc
        failures = [event for event in events
                    if event.get("type") in ("error", "turn.failed")]
        if proc.returncode or failures:
            detail = failures[-1] if failures else ""
            raise self._failure(proc, str(detail))
        thread_id = next((event.get("thread_id") for event in events
                          if event.get("type") == "thread.started"), "")
        messages = [event.get("item") or {} for event in events
                    if event.get("type") == "item.completed"
                    and (event.get("item") or {}).get("type") == "agent_message"]
        if not messages:
            raise self._failure(proc, "Codex completed without an agent message")
        text = str(messages[-1].get("text") or "")
        usage_raw = next((event.get("usage") or {} for event in reversed(events)
                          if event.get("type") == "turn.completed"), {})
        spent = Usage(
            prompt_tokens=int(usage_raw.get("input_tokens") or 0),
            completion_tokens=int(usage_raw.get("output_tokens") or 0),
            cached_tokens=int(usage_raw.get("cached_input_tokens") or 0),
            reasoning_tokens=int(usage_raw.get("reasoning_output_tokens") or 0),
            calls=1, requests=1,
        )
        reply = Reply(
            text=text, data=_parse_json(text), usage=spent,
            model=_native_model(self.provider, model),
            reasoning=_codex_reasoning(events),
            generation_id="codex-" + str(thread_id or time.time_ns()),
            finish_reason="stop",
        )
        self._charge(spent)
        return reply


#: Antigravity model ids end in the reasoning budget they were published with
#: (`gemini-3.8-flash-high`). Passing `--effort` on top of one of those is asking the
#: same question twice, so the suffix wins and the flag is only used for a model that
#: does not state a budget of its own.
AGY_EFFORT_SUFFIX = re.compile(r"-(low|medium|high)$")


class Antigravity(ProgrammaticClient):
    """Antigravity print mode as a stateless structured-completion backend."""

    provider = "antigravity"

    def complete(self, *, model: str, prefix: str, suffix: str,
                 schema: dict | None = None, schema_name: str = "diff",
                 max_tokens: int = 8000, cache_prefix: bool = True,
                 capture_reasoning: bool = False, provider=None,
                 json_object: bool = False, reasoning_effort: str | None = None,
                 turns: list[dict] | None = None,
                 service_tier: str | None = None) -> Reply:
        del schema_name, max_tokens, cache_prefix, capture_reasoning, provider
        del json_object, service_tier
        native = _native_model(self.provider, model)
        if reasoning_effort is None:
            reasoning_effort = _spec_for(self.provider, model).reasoning_effort
        args = [self.command, "--output-format", "json", "--model", native,
                "--sandbox", "--disable-slash-commands"]
        schema_path = ""
        if schema:
            # `--json-schema` takes a literal schema or a path to one. The propose
            # schema is thousands of characters and the prompt already rides in argv,
            # so the file form is what keeps a large call under the argument limit.
            fd, schema_path = tempfile.mkstemp(prefix="memcal-schema-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(schema, fh, separators=(",", ":"))
            args += ["--json-schema", schema_path]
        if reasoning_effort and not AGY_EFFORT_SUFFIX.search(native):
            args += ["--effort", reasoning_effort]
        # `agy` runs its own five-minute clock over the turn and, when it expires,
        # returns *partial output* with returncode 0 — an `ERROR` status, or worse a
        # `SUCCESS` with an empty response and a zeroed token count. Neither is a
        # refusal, and memcal was politely waiting 900s for a process that had already
        # given up at 300. A propose request with several bundles and a thinking budget
        # routinely runs past five minutes, so every request in a real pass failed while
        # a one-line probe succeeded.
        #
        # One clock, set from the same config value the subprocess timeout uses, and
        # slightly under it so the CLI reports its own timeout rather than being killed.
        args += ["--print-timeout", f"{max(30, int(self.timeout) - 30)}s"]
        # The prompt is a flag value, not stdin: bare `--print` consumes whatever comes
        # next as the prompt, so it goes last and attached, and nothing after it can be
        # read as the thing to ask.
        args.append("--print=" + _programmatic_prompt(prefix, suffix, turns))
        try:
            proc = self._run(args, "")
        except OSError as exc:
            # Everything rides in argv here, so a large packed wave can cross the
            # system's argument limit. That arrives as a bare errno; say which knob
            # shrinks the request instead.
            raise LLMError(
                "Antigravity could not be started, most likely because the prompt "
                "exceeds the system argument limit. Lower MEMCAL_PACK_TOKENS or "
                f"MEMCAL_PACK_BUNDLES. ({exc})") from exc
        finally:
            if schema_path:
                try:
                    os.unlink(schema_path)
                except OSError:
                    pass
        try:
            raw = json.loads(proc.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise self._failure(proc, f"Antigravity returned invalid JSON: {exc}") from exc
        if proc.returncode or str(raw.get("status") or "").upper() != "SUCCESS":
            raise self._failure(proc, raw.get("error") or raw.get("status") or "")
        text = str(raw.get("response") or "")
        data = raw.get("structured_output")
        if data is None:
            data = _parse_json(text)
        if data is None and not text.strip():
            # `SUCCESS` with an empty response and, usually, a zeroed token count. It is
            # what a turn cut short by the CLI's print timeout looks like from out here,
            # so say that rather than only the symptom — the previous wording sent
            # everyone looking for a model that answers nothing, when the answer was
            # that nobody waited for it. Counting it as a reply would apply an empty diff
            # and call the bundle done; raising records the failed call and re-queues it.
            raise self._failure(
                proc, "Antigravity reported success with no response — usually a turn "
                      "cut off by its print timeout. Raise MEMCAL_LLM_COMMAND_TIMEOUT, "
                      "or lower MEMCAL_PACK_BUNDLES so a turn finishes sooner")
        usage_raw = raw.get("usage") or {}
        cached = int(usage_raw.get("cache_read_tokens") or 0)
        spent = Usage(
            # `input_tokens` counts only what was sent fresh; a cache hit is reported
            # beside it rather than inside it, so a prompt read from cache would
            # otherwise vanish from the ledger entirely.
            prompt_tokens=int(usage_raw.get("input_tokens") or 0) + cached,
            completion_tokens=int(usage_raw.get("output_tokens") or 0),
            cached_tokens=cached,
            reasoning_tokens=int(usage_raw.get("thinking_tokens") or 0),
            calls=1, requests=1,
        )
        reply = Reply(
            text=text, data=data, usage=spent, model=native,
            generation_id="agy-" + str(raw.get("conversation_id") or time.time_ns()),
            # The CLI reports what thinking cost but never what it said, so the field
            # stays empty rather than being filled with the answer a second time.
            reasoning="",
            finish_reason="stop",
        )
        self._charge(spent)
        return reply


PROVIDER_DEFAULT_MODELS = {
    "openrouter": "openai/gpt-5.6-luna",
    "claude-code": "claude-sonnet-5",
    "codex": "gpt-5.6-luna",
    "antigravity": "gemini-3.8-flash-high",
}

@dataclass(frozen=True)
class CliBackend:
    """One authenticated CLI backend: its client, its command, and its setting."""
    client: type
    #: Reads the configured executable. An identifier, not a field name in a string,
    #: so a knob nothing reads is still visible as one.
    command: Callable[[object], str]
    env: str


#: Everything the CLI backends differ by, in one table, so the factory, the readiness
#: check and `memcal setup` cannot disagree about a provider.
PROVIDER_COMMANDS: dict[str, CliBackend] = {
    "claude-code": CliBackend(ClaudeCode, lambda cfg: cfg.claude_command,
                              "MEMCAL_CLAUDE_COMMAND"),
    "codex": CliBackend(Codex, lambda cfg: cfg.codex_command, "MEMCAL_CODEX_COMMAND"),
    "antigravity": CliBackend(Antigravity, lambda cfg: cfg.agy_command,
                              "MEMCAL_AGY_COMMAND"),
}

#: What a store with no `MEMCAL_LLM_PROVIDER` uses. Kept beside the table it has to
#: agree with, and matching `Config.llm_provider`.
DEFAULT_PROVIDER = "codex"


def client_for(cfg, *, on_retry: Callable[[str], None] | None = None) -> CompletionClient:
    """Construct the configured backend at the one boundary every model call uses."""
    provider = str(getattr(cfg, "llm_provider", DEFAULT_PROVIDER) or DEFAULT_PROVIDER).lower()
    if provider == "openrouter":
        return OpenRouter(cfg.api_key, on_retry=on_retry)
    backend = PROVIDER_COMMANDS.get(provider)
    if backend:
        return backend.client(backend.command(cfg), cwd=cfg.home,
                              timeout=float(getattr(cfg, "llm_command_timeout", 900)))
    raise LLMError(
        f"unknown LLM provider {provider!r}; choose "
        f"{', '.join(PROVIDER_DEFAULT_MODELS)} with `memcal setup`")


def provider_status(cfg) -> tuple[bool, str]:
    """Check whether the configured provider has its required key or executable."""
    provider = str(getattr(cfg, "llm_provider", DEFAULT_PROVIDER) or DEFAULT_PROVIDER).lower()
    if provider == "openrouter":
        return bool(cfg.api_key), "API key present" if cfg.api_key else "API key missing"
    backend = PROVIDER_COMMANDS.get(provider)
    if not backend:
        return False, f"unknown provider: {provider}"
    command = backend.command(cfg)
    path = shutil.which(command)
    return bool(path), path or f"command not found: {command}"


def _safe(worker: Callable, job):
    try:
        return worker(job)
    except Exception as exc:  # surfaced per-bundle; one bad bundle must not kill the pass
        return exc


def _reply_from(raw: dict, model: str, tally: Tally) -> Reply:
    """One response body, read into a `Reply`. Raises when there is no answer in it."""
    if "error" in raw and not raw.get("choices"):
        raise LLMError(str(raw["error"])[:400])
    choices = raw.get("choices") or []
    if not choices:
        raise LLMError(f"no choices in response: {str(raw)[:300]}")
    message = choices[0].get("message") or {}
    text = message.get("content") or ""
    reasoning = message.get("reasoning") or ""
    if not reasoning:
        blocks = message.get("reasoning_details") or []
        reasoning = "\n".join(
            b.get("text") or b.get("summary") or "" for b in blocks if isinstance(b, dict))
    return Reply(text=text, data=_parse_json(text),
                 usage=_usage_from(raw.get("usage") or {}),
                 model=raw.get("model", model), reasoning=reasoning.strip(),
                 generation_id=raw.get("id", ""),
                 # What the request was actually served at, which is not always what
                 # it asked for: a model with no flex-capable endpoint is served at
                 # standard rates with no error. Recorded rather than assumed, so
                 # "we moved to flex" is a claim `memcal trace` can check instead of
                 # a comment that quietly stops being true.
                 service_tier=str(raw.get("service_tier") or ""),
                 finish_reason=str(choices[0].get("finish_reason")
                                   or choices[0].get("native_finish_reason") or ""),
                 # What this one answer actually took. `requests > 1` says the reply was
                 # retried into existence, which prices differently from the single
                 # request the cost line implies.
                 requests=tally.requests, waited=round(tally.waited, 1))


def _usage_from(raw: dict) -> Usage:
    details = raw.get("prompt_tokens_details") or {}
    out_details = raw.get("completion_tokens_details") or {}
    return Usage(
        prompt_tokens=int(raw.get("prompt_tokens") or 0),
        completion_tokens=int(raw.get("completion_tokens") or 0),
        cached_tokens=int(details.get("cached_tokens") or 0),
        reasoning_tokens=int(out_details.get("reasoning_tokens") or 0),
        cost=float(raw.get("cost") or 0.0),
        calls=1,
    )


def _parse_json(text: str):
    """Models sometimes fence JSON even under a schema. Recover rather than fail."""
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
        end = max(text.rfind("}"), text.rfind("]"))
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                return None
        return None
