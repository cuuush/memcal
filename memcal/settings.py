"""Every knob memcal has, in one schema: what it means, and where its value lives.

`config.load` reads `MEMCAL_*` out of three `.env` files and the environment. That is a
fine way to read a setting and a poor way to change one: the variable's name is not the
thing's name, the file that wins is not always the file you edited, and nothing lists
what may be set at all. This module is the list, the validation, and the write. It
knows nothing about HTTP or launchd — `web_settings.py` assembles those around it.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from pathlib import Path

from . import config
from .config import Config

#: The file the UI writes. The others are read-only as far as this module is concerned:
#: a project checkout's `.env` and the shell's environment belong to whoever set them.
STORE_ENV = ".env"

#: A value goes onto one line of a `.env` file, so anything that could end that line
#: early would write a key nobody asked for.
_FORBIDDEN = "\n\r\x00"
MAX_VALUE_CHARS = 500
MAX_SECRET_CHARS = 4_000


class SettingsError(ValueError):
    """A rejected value, worded for the person who typed it."""


@dataclass(frozen=True)
class Group:
    id: str
    title: str
    note: str


@dataclass(frozen=True)
class Setting:
    """One knob: its variable, the `Config` field it lands on, and how to say it."""

    key: str
    attr: str
    label: str
    help: str
    group: str
    #: How the tab asks for it. `combo` is free text that knows what the usual answers
    #: are — a model, an executable, a calendar — and `stages` is the one field whose
    #: value is a list, so it is picked rather than spelled.
    kind: str = "text"              # text | int | choice | bool | combo | stages
    choices: tuple[tuple[str, str], ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    unit: str = ""
    placeholder: str = ""
    #: Read from the store's own `.env` only, never from a checkout or the working
    #: directory. `config.load` scopes the outward-write settings this way on purpose:
    #: which calendar gets written to is a property of the store, not of a shell.
    store_scoped: bool = False


GROUPS: tuple[Group, ...] = (
    Group("provider", "Model and provider",
          "Which runtime executes a model call, and which model it asks. The CLI "
          "providers reuse a login you already have; OpenRouter needs an API key."),
    Group("brief", "The brief",
          "The window memcal keeps in context, and how much of it fits."),
    Group("collect", "What gets collected",
          "How far back a source reaches, and how much of what it finds a single pass "
          "is allowed to read. Nothing here deletes anything: what a pass does not read "
          "stays archived and searchable."),
    Group("dream", "How a pass is packed",
          "One dream pass is a handful of requests, each carrying several bundles. "
          "These decide how those requests are built — the main lever on what a pass "
          "costs."),
    Group("merge", "Deciding two rows are one occasion",
          "How eagerly a proposed row is folded into one that already exists. Lower "
          "thresholds merge more, and merge wrongly more often."),
    Group("publish", "Writing back out",
          "The only settings here that write outside memcal. Both are off until you "
          "name a destination, and both are scoped to this store."),
)

#: Short on purpose: a select shows one option at a time in a narrow control, and a
#: label that has to be truncated to fit says less than a short one. What each provider
#: is, is the group's own note.
_PROVIDERS = (
    ("codex", "Codex"),
    ("claude-code", "Claude Code"),
    ("antigravity", "Antigravity"),
    ("openrouter", "OpenRouter · API key"),
)

SETTINGS: tuple[Setting, ...] = (
    # ------------------------------------------------------------------ provider --
    Setting("MEMCAL_LLM_PROVIDER", "llm_provider", "Provider",
            "Every application model call goes through this. Changing it also changes "
            "what a model name has to look like.",
            "provider", kind="choice", choices=_PROVIDERS),
    Setting("MEMCAL_PROPOSE_MODEL", "propose_model", "Propose model",
            "Reads the night's traffic and proposes what to write. This is where a "
            "pass spends most of its money.",
            "provider", kind="combo", placeholder="provider default"),
    Setting("MEMCAL_SWEEP_MODEL", "sweep_model", "Sweep model",
            "Revisits what the store already holds — stale rows, questions that "
            "answered themselves.",
            "provider", kind="combo", placeholder="provider default"),
    Setting("MEMCAL_MATCH_MODEL", "match_model", "Merge model",
            "Arbitrates whether two proposed rows are one occasion. It keeps its "
            "older name in the variable; the stage is called Merge.",
            "provider", kind="combo", placeholder="provider default"),
    Setting("MEMCAL_REASONING_EFFORT", "reasoning_effort", "Reasoning effort",
            "Overrides the per-model default. Higher costs more and is slower; on a "
            "short bundle it usually buys nothing.",
            "provider", kind="choice",
            choices=(("", "model default"), ("low", "low"), ("medium", "medium"),
                     ("high", "high"))),
    Setting("MEMCAL_MAX_PARALLEL", "max_parallel", "Requests in flight",
            "How many model requests run at once. Raise it to finish a pass sooner, "
            "lower it if the provider starts rate-limiting.",
            "provider", kind="int", minimum=1, maximum=64),
    Setting("MEMCAL_LLM_COMMAND_TIMEOUT", "llm_command_timeout", "CLI call timeout",
            "How long a CLI provider gets to answer one request before memcal gives "
            "up on it. Only used by the CLI providers.",
            "provider", kind="int", minimum=30, maximum=7_200, unit="seconds"),
    Setting("MEMCAL_CODEX_COMMAND", "codex_command", "codex executable",
            "Absolute path is safest: the nightly agent does not inherit your shell's "
            "PATH.",
            "provider", kind="combo", placeholder="codex"),
    Setting("MEMCAL_CLAUDE_COMMAND", "claude_command", "claude executable",
            "Absolute path is safest: the nightly agent does not inherit your shell's "
            "PATH.",
            "provider", kind="combo", placeholder="claude"),
    Setting("MEMCAL_AGY_COMMAND", "agy_command", "agy executable",
            "Absolute path is safest: the nightly agent does not inherit your shell's "
            "PATH.",
            "provider", kind="combo", placeholder="agy"),

    # --------------------------------------------------------------------- brief --
    Setting("MEMCAL_DAYS_BACK", "days_back", "Days back",
            "How far behind today the brief still reports on.",
            "brief", kind="int", minimum=0, maximum=90, unit="days"),
    Setting("MEMCAL_DAYS_FORWARD", "days_forward", "Days forward",
            "How far ahead the brief reaches. This is the window, not the store — "
            "everything else stays one question away.",
            "brief", kind="int", minimum=1, maximum=365, unit="days"),
    Setting("MEMCAL_BRIEF_TOKEN_CAP", "brief_token_cap", "Brief token cap",
            "The ceiling on the brief itself. It is in the assistant's context every "
            "turn, so this is a tax on every conversation, not on the nightly pass.",
            "brief", kind="int", minimum=200, maximum=20_000, unit="tokens"),

    # ------------------------------------------------------------------- collect --
    Setting("MEMCAL_SPOOL_HORIZON_DAYS", "spool_horizon_days", "Model horizon",
            "Nothing older than this is ever sent to a model. Older items are still "
            "collected, archived, and searchable — they just stop costing tokens.",
            "collect", kind="int", minimum=1, maximum=365, unit="days"),
    Setting("MEMCAL_EMAIL_BACKFILL_DAYS", "email_backfill_days", "First email reach",
            "How far back the first mail fetch goes. Later runs resume from a "
            "watermark and ignore this. 0 means the model horizon above.",
            "collect", kind="int", minimum=0, maximum=3_650, unit="days"),
    Setting("MEMCAL_PLATFORM_MUTE", "platform_mute", "Conversations you muted there",
            "What to do about a chat the platform itself reports as muted. It is "
            "evidence, not an instruction — you may have muted a group whose plans "
            "still concern you.",
            "collect", kind="choice",
            choices=(("show", "show · archive, not a signal"),
                     ("ask", "ask · put it up for review"),
                     ("mute", "mute · take their word for it"))),
    Setting("MEMCAL_ITEM_BUDGET", "item_budget", "Lines per pass",
            "The total number of spooled lines one pass may read. What does not fit "
            "waits for the next pass rather than being dropped.",
            "collect", kind="int", minimum=100, maximum=200_000, unit="lines"),
    Setting("MEMCAL_ITEMS_PER_ENTITY", "items_per_entity", "Lines per conversation",
            "The ceiling for any one person or thread, so a single busy group chat "
            "cannot eat the budget above.",
            "collect", kind="int", minimum=10, maximum=50_000, unit="lines"),
    Setting("MEMCAL_COLD_START_WAVES", "cold_start_waves", "Cold-start waves",
            "A first, huge ingest is split into this many passes, so what the early "
            "ones resolve is available to the later ones.",
            "collect", kind="int", minimum=1, maximum=20),

    # --------------------------------------------------------------------- dream --
    Setting("MEMCAL_PACK_BUNDLES", "pack_bundles", "Bundles per request",
            "How many conversations ride in one request. More means fewer, larger "
            "requests.",
            "dream", kind="int", minimum=1, maximum=64),
    Setting("MEMCAL_PACK_TOKENS", "pack_tokens", "Tokens per request",
            "The size ceiling for one request, whichever comes first with the bundle "
            "count above.",
            "dream", kind="int", minimum=1_000, maximum=400_000, unit="tokens"),
    Setting("MEMCAL_PACK_STRATEGY", "pack_strategy", "Packing strategy",
            "Affinity puts conversations that look like they are about the same "
            "occasion in one request, so the model can see they are.",
            "dream", kind="choice",
            choices=(("size", "size · by token size"),
                     ("affinity", "affinity · by what relates"))),
    Setting("MEMCAL_AFFINITY_NEAR_DAYS", "affinity_near_days", "Affinity window",
            "How far apart two references may be and still count as the same "
            "occasion. Only used by the affinity strategy.",
            "dream", kind="int", minimum=0, maximum=30, unit="days"),
    Setting("MEMCAL_PROMPT_VERSION", "prompt_version", "Propose prompt",
            "v2 asks for a list of the bundles it reviewed plus diffs only where "
            "something changed, which is both cheaper and checkable.",
            "dream", kind="choice",
            choices=(("v2", "v2 · reviewed + diffs"),
                     ("v1", "v1 · a diff per bundle"))),
    Setting("MEMCAL_PROPOSE_STAGES", "propose_stages", "Propose stages",
            "Pick none and one answer covers everything. Pick some and the same "
            "bundles are read once per stage — more calls, more attention on each, and "
            "each stage can see what the ones before it wrote. They always run in the "
            "order shown.",
            "dream", kind="stages", placeholder="off — one call"),
    Setting("MEMCAL_BUNDLE_FORMAT", "bundle_format", "Bundle wire format",
            "How a bundle is laid out in the prompt. The quiet variant drops the "
            "stream tag from every line of a single-stream bundle.",
            "dream", kind="choice",
            choices=(("v1", "v1 · a tag on every line"),
                     ("v2-quiet-stream", "v2 · quiet stream tags"))),

    # --------------------------------------------------------------------- merge --
    Setting("MEMCAL_SAME_EVENT_TOKENS", "same_event_tokens", "Title words that match",
            "How many distinctive title words two rows must share before they are "
            "considered the same occasion.",
            "merge", kind="int", minimum=1, maximum=8, unit="words"),
    Setting("MEMCAL_SAME_EVENT_POOR_TOKENS", "same_event_poor_tokens",
            "…when there is nothing else to go on",
            "The same threshold for a row with no participants and no location, where "
            "the title is all the evidence there is.",
            "merge", kind="int", minimum=1, maximum=8, unit="words"),

    # ------------------------------------------------------------------- publish --
    Setting("MEMCAL_PUBLISH_CALENDAR", "publish_calendar", "Publish to calendar",
            "The name of a macOS calendar to write confirmed commitments into. Empty "
            "means memcal never writes to your calendar.",
            "publish", kind="combo", placeholder="off", store_scoped=True),
    Setting("MEMCAL_PUBLISH_REMINDERS", "publish_reminders", "Publish to Reminders",
            "The name of a Reminders list for to-do reminder timestamps. Empty means "
            "memcal never writes to Reminders.",
            "publish", kind="combo", placeholder="off", store_scoped=True),
    Setting("MEMCAL_REMIND_DEADLINES", "remind_deadlines", "Schedule deadline reminders",
            "Whether an obligation with a deadline gets a reminder timestamp at all. "
            "With no Reminders list named above, this stays internal.",
            # Not store-scoped, unlike the two above it: `config.load` reads this one
            # from the merged environment. Claiming otherwise would print a badge that
            # is false and suppress the warning that a checkout's `.env` outranks this.
            "publish", kind="bool"),
)

BY_KEY: dict[str, Setting] = {s.key: s for s in SETTINGS}

_FIELD_DEFAULTS = {f.name: f.default for f in dataclasses.fields(Config)}

#: `config.load` fills these from the chosen provider when nothing sets them, so the
#: dataclass default is not what an unset store actually runs. Reporting the dataclass
#: value here would mark all three as changed on a store that has changed nothing.
_PROVIDER_MODEL_ATTRS = ("propose_model", "sweep_model", "match_model")


# ------------------------------------------------------------------- reading --

def default_text(setting: Setting, cfg: Config | None = None) -> str:
    """What memcal falls back to, written the way the form writes it."""
    if cfg is not None and setting.attr in _PROVIDER_MODEL_ATTRS:
        from . import llm                                          # noqa: PLC0415
        native = llm.PROVIDER_DEFAULT_MODELS.get(
            str(getattr(cfg, "llm_provider", "") or "").lower())
        if native:
            return native
    value = _FIELD_DEFAULTS.get(setting.attr, "")
    if setting.kind == "bool":
        return "on" if value else "off"
    return "" if value is None else str(value)


def current_text(cfg: Config, setting: Setting) -> str:
    value = getattr(cfg, setting.attr, None)
    if setting.kind == "bool":
        return "on" if value else "off"
    return "" if value is None else str(value)


def env_files(cfg: Config) -> list[tuple[str, Path]]:
    """Every `.env` `config.load` reads, highest precedence first.

    The order matters and is easy to get backwards: `load_env` merges left to right, so
    the *last* file read wins, which makes the working directory outrank the store. A
    settings page that wrote the store's file and said nothing about that would be
    lying by omission the moment someone kept a `.env` beside a checkout.
    """
    def canonical(path: Path) -> Path:
        try:
            return path.expanduser().resolve()
        except OSError:                       # a deleted cwd, mostly
            return path.expanduser()

    store = canonical(cfg.home / STORE_ENV)
    seen: set[Path] = set()
    out: list[tuple[str, Path]] = []
    for role, path in (("working directory", Path.cwd() / STORE_ENV),
                       ("store", cfg.home / STORE_ENV),
                       ("project", config.PROJECT_ROOT / STORE_ENV)):
        resolved = canonical(path)
        if resolved in seen:
            continue
        seen.add(resolved)
        # The store's own file answers to that name wherever else it turns up. Run
        # `memcal web` from inside `~/.memcal` and it is also the working directory's
        # file; labelled that way it stopped being the store row, so every save warned
        # that the file it had just written was shadowing itself, and a store-scoped
        # setting — which is only ever read from the store row — reported no value at all.
        out.append(("store" if resolved == store else role, path))
    return out


def _file_values(cfg: Config) -> list[tuple[str, Path, dict[str, str]]]:
    return [(role, path, config.load_env(path)) for role, path in env_files(cfg)]


def origin(setting: Setting, files: list[tuple[str, Path, dict[str, str]]]) -> dict:
    """Where the effective value comes from, and what outranks the file we write.

    `config.load` treats an empty value as absent (`env.get(name) or os.environ...`),
    so a blank line here is a reset rather than an override, and is reported as one.
    """
    for role, path, values in files:
        if setting.store_scoped and role != "store":
            continue
        if values.get(setting.key):
            return {"origin": role, "origin_path": str(path)}
    if os.environ.get(setting.key):
        return {"origin": "environment", "origin_path": ""}
    return {"origin": "default", "origin_path": ""}


def shadowed_by(setting: Setting, files: list[tuple[str, Path, dict[str, str]]]) -> str:
    """A file that outranks the store's `.env` and already sets this key."""
    for role, path, values in files:
        if role == "store":
            return ""
        if not setting.store_scoped and values.get(setting.key):
            return str(path)
    return ""


def stage_names() -> list[str]:
    """The stages `propose_stages` may name, in the order they have to run in."""
    from .dream import stages                                      # noqa: PLC0415
    return list(stages.DEFAULT_ORDER)


def snapshot(cfg: Config) -> dict:
    """Every setting, its value, its default, and which file that value came from."""
    files = _file_values(cfg)
    groups = []
    for group in GROUPS:
        rows = []
        for setting in SETTINGS:
            if setting.group != group.id:
                continue
            value, default = current_text(cfg, setting), default_text(setting, cfg)
            rows.append({
                "key": setting.key, "attr": setting.attr, "label": setting.label,
                "help": setting.help, "kind": setting.kind,
                "choices": [{"value": v, "label": text} for v, text in setting.choices],
                # For the one list-valued field, the stages themselves — named by the
                # module that runs them, so a renamed stage cannot linger in the UI.
                "options": stage_names() if setting.kind == "stages" else [],
                "min": setting.minimum, "max": setting.maximum,
                "unit": setting.unit, "placeholder": setting.placeholder,
                "scope": "store" if setting.store_scoped else "everywhere",
                "value": value, "default": default,
                "custom": value != default,
                "shadowed_by": shadowed_by(setting, files),
                **origin(setting, files),
            })
        groups.append({"id": group.id, "title": group.title, "note": group.note,
                       "settings": rows})
    return {
        "groups": groups,
        "env_file": str(cfg.home / STORE_ENV),
        "files": [{"role": role, "path": str(path), "exists": path.is_file(),
                   "keys": len([k for k in values if k in BY_KEY])}
                  for role, path, values in files],
    }


# ------------------------------------------------------------------- writing --

def check_text(value, *, limit: int = MAX_VALUE_CHARS) -> str:
    text = str(value if value is not None else "").strip()
    if any(ch in text for ch in _FORBIDDEN):
        raise SettingsError("a value cannot contain a line break")
    if len(text) > limit:
        raise SettingsError(f"a value cannot be longer than {limit} characters")
    return text


def coerce(setting: Setting, raw, *, provider: str = "") -> tuple[str, object]:
    """`(what to store, what to set on the live config)`.

    An empty string is how the form says "unset this", for every kind of setting: the
    key is written blank, `config.load` skips it on the next start, and the running
    process goes back to the default now — including the provider-native model default,
    which is the one a restart would actually produce.
    """
    text = check_text(raw)
    if not text:
        if provider and setting.attr in _PROVIDER_MODEL_ATTRS:
            from . import llm                                      # noqa: PLC0415
            native = llm.PROVIDER_DEFAULT_MODELS.get(provider.lower())
            if native:
                return "", native
        return "", _FIELD_DEFAULTS.get(setting.attr)
    if setting.kind == "int":
        try:
            number = int(text)
        except ValueError:
            raise SettingsError(f"{setting.label} takes a whole number, not {text!r}")
        if setting.minimum is not None and number < setting.minimum:
            raise SettingsError(f"{setting.label} cannot be below {setting.minimum}")
        if setting.maximum is not None and number > setting.maximum:
            raise SettingsError(f"{setting.label} cannot be above {setting.maximum}")
        return str(number), number
    if setting.kind == "bool":
        if text.lower() in ("on", "1", "yes", "true"):
            return "1", True
        if text.lower() in ("off", "0", "no", "false"):
            return "0", False
        raise SettingsError(f"{setting.label} is on or off, not {text!r}")
    if setting.kind == "choice":
        allowed = [value for value, _label in setting.choices]
        if text not in allowed:
            raise SettingsError(
                f"{setting.label} is one of {', '.join(v or 'empty' for v in allowed)}")
        return text, text
    if setting.kind == "stages":
        # The tab picks these rather than spelling them, but the field is still a list
        # in a file: a value typed elsewhere, or a stage renamed since, fails the next
        # pass at the point where it has already spent the collect.
        from .dream import stages                                  # noqa: PLC0415
        try:
            stages.parse(text)
        except stages.UnknownStage as exc:
            raise SettingsError(str(exc)) from exc
    return text, text


def _check_models_served(provider: str, planned: dict[str, tuple[str, object]]) -> None:
    """Refuse a model that is known to belong to a *different* provider.

    Run 29 read nothing: Codex was selected while the propose model still said
    `gemini-3.8-flash-high`, and all 74 bundles were refused. Narrow on purpose —
    `llm.belongs_elsewhere` answers only when it recognises the model as someone else's,
    so a model memcal has never heard of stays typable and a release that outpaces
    `llm.PRICES` is not locked out.
    """
    from . import llm                                              # noqa: PLC0415
    for key, (text, _value) in planned.items():
        if BY_KEY[key].attr not in _PROVIDER_MODEL_ATTRS or not text:
            continue
        owner = llm.belongs_elsewhere(provider, text)
        if owner:
            raise SettingsError(
                f"{text} belongs to {owner}, and the provider is {provider}, so every "
                f"request in a pass would be refused. Switch the provider to {owner}, "
                f"pick one of {provider}'s own, or leave the field empty for "
                f"{llm.PROVIDER_DEFAULT_MODELS.get(provider, 'its default')}.")


def write_env(path: Path, values: dict[str, str]) -> None:
    """Update owned keys without flattening a person's hand-edited .env file."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    pending = dict(values)
    out = []
    for line in lines:
        stripped = line.strip()
        key = stripped.partition("=")[0].strip() if "=" in stripped else ""
        if key in pending:
            out.append(f"{key}={pending.pop(key)}")
        else:
            out.append(line)
    if pending and out and out[-1].strip():
        out.append("")
    out.extend(f"{key}={value}" for key, value in pending.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def save(cfg: Config, changes: dict) -> dict:
    """Validate everything, then write once, then apply to the running process.

    All-or-nothing on purpose: half a form landing because the fourth field had a typo
    leaves the store in a state nobody chose.
    """
    if not isinstance(changes, dict):
        raise SettingsError("expected an object of setting keys")
    unknown = [key for key in changes if key not in BY_KEY]
    if unknown:
        raise SettingsError(f"not a memcal setting: {', '.join(sorted(unknown))}")

    # Switching provider and clearing a model in the same save is one intention —
    # "use whatever the new provider uses" — so the new provider decides what an
    # emptied model field falls back to.
    provider = str(changes.get("MEMCAL_LLM_PROVIDER")
                   or getattr(cfg, "llm_provider", "") or "")
    planned: dict[str, tuple[str, object]] = {}
    for key, raw in changes.items():
        planned[key] = coerce(BY_KEY[key], raw, provider=check_text(provider))
    _check_models_served(check_text(provider), planned)

    files = _file_values(cfg)
    write_env(cfg.home / STORE_ENV, {key: text for key, (text, _v) in planned.items()})

    warnings = []
    for key, (text, value) in planned.items():
        setting = BY_KEY[key]
        setattr(cfg, setting.attr, value)
        # `cfg.env` is the merged view every credential lookup reads. Keeping it in step
        # means the rest of this process sees what was just saved without a restart.
        if text:
            cfg.env[key] = text
        else:
            cfg.env.pop(key, None)
        blocker = shadowed_by(setting, files)
        if blocker:
            warnings.append(
                f"{setting.label} is also set in {blocker}, which wins the next time "
                f"memcal starts. This process is using the new value now.")
    resolve_provider_models(cfg)
    return {"saved": sorted(planned), "warnings": warnings}


def resolve_provider_models(cfg: Config) -> None:
    """Re-run the last thing `config.load` does: fill unset models from the provider.

    Changing only the provider changes three other values, because a model nobody set
    follows whichever runtime is chosen. Without this the live config kept the previous
    provider's model id and the next pass handed a Claude Code CLI an OpenRouter-shaped
    name — a save that is correct on disk and wrong in the process that wrote it.
    """
    from . import llm                                              # noqa: PLC0415
    native = llm.PROVIDER_DEFAULT_MODELS.get(
        str(getattr(cfg, "llm_provider", "") or "").strip().lower())
    if not native:
        return
    for setting in SETTINGS:
        if setting.attr not in _PROVIDER_MODEL_ATTRS:
            continue
        # Configured anywhere `config.load` looks means the choice was explicit and
        # stands; `cfg.env` is kept in step with the file this module writes.
        if not (cfg.env.get(setting.key) or os.environ.get(setting.key)):
            setattr(cfg, setting.attr, native)


# --------------------------------------------------------------- credentials --

def credentials(cfg: Config) -> list[dict]:
    """Which credentials memcal looks for, who wants them, and whether one is there.

    Presence only. A page that can read a token back out is a page that leaks it to
    anything that can reach the port.
    """
    from . import sources                                          # noqa: PLC0415
    wanted: dict[str, list[str]] = {}
    for source in sources.all_sources(cfg):
        for name in getattr(source, "secrets", ()) or ():
            wanted.setdefault(name, []).append(source.name)
    if str(getattr(cfg, "llm_provider", "")).lower() == "openrouter":
        wanted.setdefault("OPENROUTER_API_KEY", []).append("openrouter")
    return [{"name": name, "used_by": users,
             "present": bool(cfg.secret(name, name.lower()))}
            for name, users in sorted(wanted.items())]


def save_credential(cfg: Config, name: str, value) -> dict:
    """Set or clear one credential, by name, from the set sources actually ask for."""
    name = check_text(name, limit=120).upper()
    known = {row["name"] for row in credentials(cfg)}
    if name not in known:
        raise SettingsError(
            f"{name} is not a credential any source asks for. "
            f"Known: {', '.join(sorted(known)) or 'none'}")
    secret = check_text(value, limit=MAX_SECRET_CHARS)
    write_env(cfg.home / STORE_ENV, {name: secret})
    if secret:
        cfg.env[name] = secret
    else:
        cfg.env.pop(name, None)
    return {"name": name, "present": bool(secret)}
