"""Paths, settings, and .env loading."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOME = Path.home() / ".memcal"


def load_env(*paths: Path) -> dict[str, str]:
    """Read .env files into a dict without modifying os.environ.

    A bare key line without '=' starting with 'sk-or-' is mapped to OPENROUTER_API_KEY.
    """
    out: dict[str, str] = {}
    for path in paths:
        if not path or not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                value = value.strip().strip("'\"")
                out[key.strip()] = value
            elif line.startswith("sk-or-"):
                out["OPENROUTER_API_KEY"] = line
    return out


@dataclass
class Config:
    home: Path
    env: dict[str, str] = field(default_factory=dict)

    # Completion transport. OpenRouter uses an API key; the CLI providers use the
    # caller's existing Claude Code or Codex authentication.
    llm_provider: str = "openrouter"
    claude_command: str = "claude"
    codex_command: str = "codex"
    llm_command_timeout: int = 900

    # Temporal window and token budget for the brief context.
    days_back: int = 3
    days_forward: int = 7
    brief_token_cap: int = 1500

    # Model ingestion horizon in days; older items remain archived and searchable.
    spool_horizon_days: int = 30

    # Total line ceiling (`item_budget`) and per-entity line ceiling (`items_per_entity`).
    item_budget: int = 20_000
    items_per_entity: int = 2_000

    # Request bundle count and token ceiling for prompt packing.
    pack_bundles: int = 6
    pack_tokens: int = 12_000

    # Policy for platform-muted conversations:
    #   show  - archive and display without treating as high-priority signal (default)
    #   ask   - enqueue muted conversations for review
    #   mute  - drop or ignore muted conversations
    platform_mute: str = "show"

    # Propose prompt schema version:
    #   v1  one diff per bundle echoing the BUNDLE line
    #   v2  a `reviewed` list of bundle IDs plus diffs for changed bundles
    prompt_version: str = "v2"

    # Memory extraction stage configuration: empty for single call, 'on' for default stages,
    # or a comma-separated list of stage names.
    propose_stages: str = ""

    # Bundle wire layout format:
    #   v1               standard format with stream tag on every line
    #   v2-quiet-stream  omits redundant stream tags on single-stream bundles, retaining 'agent'
    bundle_format: str = "v1"

    # Default model identifiers for propose, sweep, and match operations.
    propose_model: str = "openai/gpt-5.6-luna"
    sweep_model: str = "openai/gpt-5.6-luna"
    match_model: str = "openai/gpt-5.6-luna"

    #: Override for model reasoning effort ('low', 'medium', 'high'). Empty string defaults
    #: to the model configuration in llm.ENDPOINTS.
    reasoning_effort: str = ""

    # Maximum lookback days for initial email fetch. 0 defaults to spool_horizon_days.
    # Subsequent runs use watermarks.
    email_backfill_days: int = 0

    # Target calendar name for publishing confirmed commitments. Disabled when empty.
    publish_calendar: str = ""

    # Target EventKit Reminders list name for publishing to-do reminder timestamps.
    # Disabled when empty to avoid writing external state by default.
    publish_reminders: str = ""

    # Automatically schedule reminder timestamps for obligations with deadlines.
    remind_deadlines: bool = True

    # Number of execution waves to split cold-start ingestion into, allowing intermediate
    # state resolution across independent threads.
    cold_start_waves: int = 4

    # Request packing strategy:
    #   size      group bundles by token size
    #   affinity  group conversations sharing dates, keywords, and participants
    pack_strategy: str = "size"

    # Maximum day separation between references for affinity grouping.
    affinity_near_days: int = 3

    # Enable cross-referencing between bundles packed into the same request.
    pack_cross_reference: bool = False

    # Verification mode for checking model extractions against cited evidence:
    #   off    accept model extractions directly
    #   flag   detect and record evidence contradictions without altering writes
    #   reask  prompt model with specific contradiction diagnostics for correction
    verify: str = "off"

    # Maximum number of verification re-ask calls permitted per pass.
    verify_budget: int = 8

    # Distinctive title word overlap threshold for duplicate event clustering.
    # `same_event_poor_tokens` applies when either candidate row lacks participants and location.
    same_event_tokens: int = 2
    same_event_poor_tokens: int = 1

    # Maximum concurrent API requests in flight.
    max_parallel: int = 8

    @property
    def db_path(self) -> Path:
        return self.home / "memcal.db"

    @property
    def wiki_dir(self) -> Path:
        return self.home / "wiki"

    @property
    def brief_path(self) -> Path:
        return self.home / "brief.md"

    @property
    def plugin_dir(self) -> Path:
        """Drop a .py here and it becomes a source. No packaging, no install."""
        return self.home / "plugins"

    @property
    def api_key(self) -> str | None:
        return self.secret("OPENROUTER_API_KEY", "openrouter")

    def secret(self, *names: str) -> str | None:
        """Look up a credential by any of several names, case- and separator-insensitive.

        The .env here is hand-edited, so `groupme=`, `GROUPME=`, and
        `GROUPME_ACCESS_TOKEN=` all have to mean the same thing.
        """
        def norm(text: str) -> str:
            return "".join(ch for ch in text.lower() if ch.isalnum())

        wanted = {norm(name) for name in names}
        for source in (self.env, os.environ):
            for key, value in source.items():
                if value and norm(key) in wanted:
                    return value.strip()
        # Then a one-directional prefix match: BLUEBUBBLES_PASSWORD satisfies the alias
        # "bluebubbles". Only this direction — a short env name must never satisfy a
        # long alias, or `_` (which normalizes to "") would answer every lookup.
        aliases = {w for w in wanted if len(w) >= 5}
        for source in (self.env, os.environ):
            for key, value in source.items():
                nk = norm(key)
                if value and nk and any(nk.startswith(alias) for alias in aliases):
                    return value.strip()
        return None

    def ensure_dirs(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        for sub in ("people", "places", "projects", "preferences"):
            (self.wiki_dir / sub).mkdir(parents=True, exist_ok=True)
        self.plugin_dir.mkdir(parents=True, exist_ok=True)


def load(home: str | os.PathLike[str] | None = None) -> Config:
    home_path = Path(home or os.environ.get("MEMCAL_HOME") or DEFAULT_HOME).expanduser()
    env = load_env(PROJECT_ROOT / ".env", home_path / ".env", Path.cwd() / ".env")
    def _flag(raw: str) -> bool:
        """`0`/`no`/`false`/`off` are false; anything else set at all is true.

        `bool("0")` is True, so casting a flag with `bool` turns every attempt to switch
        one *off* into switching it on.
        """
        return raw.strip().lower() not in ("0", "no", "false", "off", "")

    cfg = Config(home=home_path, env=env)
    for name, attr, cast in (
        ("MEMCAL_LLM_PROVIDER", "llm_provider", str),
        ("MEMCAL_CLAUDE_COMMAND", "claude_command", str),
        ("MEMCAL_CODEX_COMMAND", "codex_command", str),
        ("MEMCAL_LLM_COMMAND_TIMEOUT", "llm_command_timeout", int),
        ("MEMCAL_PROPOSE_MODEL", "propose_model", str),
        ("MEMCAL_SWEEP_MODEL", "sweep_model", str),
        ("MEMCAL_MATCH_MODEL", "match_model", str),
        ("MEMCAL_REASONING_EFFORT", "reasoning_effort", str),
        ("MEMCAL_DAYS_BACK", "days_back", int),
        ("MEMCAL_DAYS_FORWARD", "days_forward", int),
        ("MEMCAL_BRIEF_TOKEN_CAP", "brief_token_cap", int),
        ("MEMCAL_MAX_PARALLEL", "max_parallel", int),
        ("MEMCAL_SPOOL_HORIZON_DAYS", "spool_horizon_days", int),
        ("MEMCAL_EMAIL_BACKFILL_DAYS", "email_backfill_days", int),
        ("MEMCAL_COLD_START_WAVES", "cold_start_waves", int),
        ("MEMCAL_ITEM_BUDGET", "item_budget", int),
        ("MEMCAL_ITEMS_PER_ENTITY", "items_per_entity", int),
        ("MEMCAL_PACK_BUNDLES", "pack_bundles", int),
        ("MEMCAL_PACK_TOKENS", "pack_tokens", int),
        ("MEMCAL_PLATFORM_MUTE", "platform_mute", str),
        ("MEMCAL_PROMPT_VERSION", "prompt_version", str),
        ("MEMCAL_PROPOSE_STAGES", "propose_stages", str),
        ("MEMCAL_BUNDLE_FORMAT", "bundle_format", str),
        ("MEMCAL_PACK_STRATEGY", "pack_strategy", str),
        ("MEMCAL_AFFINITY_NEAR_DAYS", "affinity_near_days", int),
        ("MEMCAL_PACK_CROSS_REFERENCE", "pack_cross_reference", _flag),
        ("MEMCAL_VERIFY", "verify", str),
        ("MEMCAL_VERIFY_BUDGET", "verify_budget", int),
        ("MEMCAL_SAME_EVENT_TOKENS", "same_event_tokens", int),
        ("MEMCAL_SAME_EVENT_POOR_TOKENS", "same_event_poor_tokens", int),
        ("MEMCAL_PUBLISH_CALENDAR", "publish_calendar", str),
        ("MEMCAL_PUBLISH_REMINDERS", "publish_reminders", str),
        ("MEMCAL_REMIND_DEADLINES", "remind_deadlines", _flag),
    ):
        raw = env.get(name) or os.environ.get(name)
        if raw:
            setattr(cfg, attr, cast(raw))

    # Provider-native defaults make `MEMCAL_LLM_PROVIDER=claude-code` sufficient on
    # its own. A stage-specific model remains authoritative when it was configured.
    defaults = {
        "claude-code": "claude-sonnet-5",
        "codex": "gpt-5.6-luna",
    }
    default_model = defaults.get(cfg.llm_provider)
    if default_model:
        for env_name, attr in (
            ("MEMCAL_PROPOSE_MODEL", "propose_model"),
            ("MEMCAL_SWEEP_MODEL", "sweep_model"),
            ("MEMCAL_MATCH_MODEL", "match_model"),
        ):
            if not (env.get(env_name) or os.environ.get(env_name)):
                setattr(cfg, attr, default_model)
    return cfg
