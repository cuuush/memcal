"""Interactive configuration using the same settings schema as the web UI."""

from __future__ import annotations

import getpass
from urllib.parse import urlsplit

from . import llm, settings
from .config import Config

SECTIONS = ("all", "provider", "imessage", "brief", "collect", "dream", "merge",
            "publish", "credentials")
IMESSAGE_KEYS = ("MEMCAL_IMESSAGE_BACKEND", "MEMCAL_IMESSAGE_FALLBACK",
                "MEMCAL_BLUEBUBBLES_LOCATION", "MEMCAL_BLUEBUBBLES_AUTOSTART")
PASSWORD_NAMES = ("BLUEBUBBLES_PASSWORD", "bluebubbles", "bluebubblespassword")
URL_NAMES = ("BLUEBUBBLES_URL", "bluebubblesurl")
CREDENTIAL_NAMES = {
    "BLUEBUBBLES_PASSWORD": PASSWORD_NAMES,
    "OPENROUTER_API_KEY": ("OPENROUTER_API_KEY", "openrouter"),
    "GROUPME_ACCESS_TOKEN": ("GROUPME_ACCESS_TOKEN", "groupme", "groupmetoken"),
    "SLACK_TOKEN": ("SLACK_TOKEN", "slack"),
    "PROTON_BRIDGE_USER": ("PROTON_BRIDGE_USER", "protonuser"),
    "PROTON_BRIDGE_PASSWORD": ("PROTON_BRIDGE_PASSWORD", "protonpassword"),
}


def _normalized(name: str) -> str:
    return "".join(c for c in name.lower() if c.isalnum())


def replace_value(cfg: Config, values: dict[str, str], names: tuple[str, ...], value: str) -> None:
    values[names[0]] = value
    aliases = {_normalized(name) for name in names}
    for key in cfg.env:
        if key != names[0] and _normalized(key) in aliases:
            values[key] = ""


def prompt(setting: settings.Setting, current: str, provider: str) -> str:
    if setting.choices:
        print(f"\n{setting.label}:")
        for index, (value, label) in enumerate(setting.choices, 1):
            print(f"  {index}. {label}" + (" (current)" if value == current else ""))
    suffix = f" {setting.unit}" if setting.unit else ""
    while True:
        answer = input(f"{setting.label} [{current or '(empty)'}{suffix}]: ").strip()
        if not answer:
            return current
        if answer == "?":
            print(setting.help)
            continue
        if setting.choices and answer.isdigit() and 1 <= int(answer) <= len(setting.choices):
            answer = setting.choices[int(answer) - 1][0]
        elif answer == "-":
            answer = ""
        try:
            text, _ = settings.coerce(setting, answer, provider=provider)
            if setting.attr in settings._PROVIDER_MODEL_ATTRS:
                owner = llm.belongs_elsewhere(provider, text)
                if owner:
                    raise settings.SettingsError(f"this model belongs to {owner}, not {provider}")
            return text
        except settings.SettingsError as exc:
            print(f"Invalid value: {exc}")


def _credential(cfg: Config, values: dict[str, str], names: tuple[str, ...], label: str,
                *, required: bool = False) -> None:
    current = cfg.secret(*names) or ""
    hint = ("configured; Enter to keep" if current else
            "required" if required else "not configured; Enter to skip")
    while True:
        answer = getpass.getpass(f"{label} [{hint}]: ").strip()
        if not answer:
            if required and not current:
                print(f"{label} is required for this backend.")
                continue
            return
        try:
            answer = settings.check_text("" if answer == "-" else answer,
                                         limit=settings.MAX_SECRET_CHARS)
        except settings.SettingsError as exc:
            print(f"Invalid value: {exc}")
            continue
        if required and not answer:
            print(f"{label} is required for this backend.")
            continue
        replace_value(cfg, values, names, answer)
        return


def _imessage(cfg: Config, values: dict[str, str]) -> None:
    print("\niMessage")
    provider = values.get("MEMCAL_LLM_PROVIDER", cfg.llm_provider)
    backend_setting = settings.BY_KEY[IMESSAGE_KEYS[0]]
    backend = prompt(backend_setting, cfg.imessage_backend, provider)
    if backend != cfg.imessage_backend:
        values[backend_setting.key] = backend
    if backend != "bluebubbles":
        print("Reads ~/Library/Messages/chat.db; the process needs Full Disk Access.")
        return
    current_url = cfg.secret(*URL_NAMES) or "http://localhost:1234"
    while True:
        url = input(f"BlueBubbles server URL [{current_url}]: ").strip() or current_url
        try:
            url = settings.check_text(url)
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise settings.SettingsError("use an http:// or https:// server URL")
        except (ValueError, settings.SettingsError) as exc:
            print(f"Invalid value: {exc}")
            continue
        if url != current_url:
            replace_value(cfg, values, URL_NAMES, url)
        break
    _credential(cfg, values, PASSWORD_NAMES, "BlueBubbles password", required=True)
    for key in IMESSAGE_KEYS[1:]:
        setting = settings.BY_KEY[key]
        value = prompt(setting, settings.current_text(cfg, setting), provider)
        if settings.coerce(setting, value, provider=provider)[1] != getattr(cfg, setting.attr):
            values[key] = value


def collect(cfg: Config, section: str, values: dict[str, str]) -> None:
    provider = values.get("MEMCAL_LLM_PROVIDER", cfg.llm_provider)
    backend = llm.PROVIDER_COMMANDS.get(provider)
    if section in {"all", "imessage", "collect"}:
        _imessage(cfg, values)
    covered = {"MEMCAL_LLM_PROVIDER", "MEMCAL_PROPOSE_MODEL", "MEMCAL_OPENAI_BASE_URL"}
    for group in settings.GROUPS:
        if section != "all" and section != group.id:
            continue
        print(f"\n{group.title}")
        for setting in settings.SETTINGS:
            if setting.group != group.id or setting.key in covered or setting.key in IMESSAGE_KEYS:
                continue
            if setting.key.endswith("_COMMAND") and (not backend or setting.key != backend.env):
                continue
            current = values.get(setting.key, settings.current_text(cfg, setting))
            value = prompt(setting, current, provider)
            if value != current:
                values[setting.key] = value
    if section == "credentials" or (section == "all" and input(
            "\nConfigure other source credentials? [y/N]: ").strip().lower() in {"y", "yes"}):
        print("\nSource credentials (hidden; '-' clears a saved value)")
        for row in settings.credentials(cfg):
            name = row["name"]
            if section != "credentials" and name in {
                    PASSWORD_NAMES[0], "OPENROUTER_API_KEY", "OPENAI_COMPAT_API_KEY"}:
                continue
            _credential(cfg, values, CREDENTIAL_NAMES.get(name, (name,)),
                        f"{', '.join(row['used_by'])}: {name}")


def confirm(cfg: Config, values: dict[str, str]) -> dict[str, str] | None:
    planned = settings.prepare(cfg, {key: value for key, value in values.items()
                                     if key in settings.BY_KEY})
    normalized = {key: settings.check_text(value, limit=settings.MAX_SECRET_CHARS)
                  for key, value in values.items() if key not in planned}
    normalized.update({key: text for key, (text, _) in planned.items()})
    print("\nChanges:")
    count = 0
    for key, (text, value) in planned.items():
        setting = settings.BY_KEY[key]
        if value == getattr(cfg, setting.attr):
            continue
        count += 1
        old = settings.current_text(cfg, setting) or "(empty)"
        new = ("on" if value else "off") if setting.kind == "bool" else text or "(default)"
        print(f"  {setting.label}: {old} → {new}")
    aliases = {_normalized(alias) for names in (*CREDENTIAL_NAMES.values(), URL_NAMES)
               for alias in names}
    for key, value in normalized.items():
        if key in planned or (not value and _normalized(key) in aliases and
                             key not in CREDENTIAL_NAMES and key != URL_NAMES[0]):
            continue
        count += 1
        if key == URL_NAMES[0]:
            print(f"  BlueBubbles server URL: {cfg.secret(*URL_NAMES) or 'http://localhost:1234'} → {value}")
        else:
            print(f"  {key}: {'updated' if value else 'cleared'}")
    if not count:
        print("  None")
    answer = input("Save? [Y/n]: ").strip().lower()
    return normalized if answer in {"", "y", "yes"} else None
