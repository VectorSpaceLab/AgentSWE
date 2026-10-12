"""Role-based provider configuration (layout contract v1, section 0.3).

Every model call in AgentSWE belongs to exactly one role:

  BUILDER  the coding agent under test (Codex CLI)
  RUNTIME  the model the built candidate agent calls while it is evaluated
  JUDGE    evaluator-side models (Result judge, tau3 user simulator and NL judge,
           PinchBench LLM grader)
  SEARCH   the Serper-compatible search API (T2 tasks only)

For a model role R the variables are ``AGENTSWE_R_{BASE_URL,API_KEY,WIRE,MODEL,EFFORT}``.
``BASE_URL``/``API_KEY``/``WIRE`` fall back to ``AGENTSWE_DEFAULT_*`` so a single
provider needs three lines.  ``MODEL``/``EFFORT`` fall back to the Lite protocol
defaults below.  Values are read from the process environment, optionally
overlaid by a dotenv file; this module never prints or logs a secret value:
``describe()`` only reports whether a key is set.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

MODEL_ROLES = ("builder", "runtime", "judge")
ROLES = MODEL_ROLES + ("search",)
WIRES = ("responses", "chat")
SEARCH_WIRES = ("serper", "legacy-proxy")

# Protocol defaults of the paper's AgentSWE-Lite runs.
# They are protocol choices, not provider knobs: changing them changes the
# experiment.  EFFORT is still an env variable because the *spelling* of an
# effort level is provider-specific (e.g. "max" vs "xhigh").
LITE_DEFAULTS = {
    "builder": {"model": "deepseek-flash", "effort": "max"},
    "runtime": {"model": "deepseek-flash", "effort": "high"},
    "judge": {"model": "deepseek-flash", "effort": "max"},
}
DEFAULT_SEARCH_WIRE = "serper"

SECRET_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET")


class ConfigError(ValueError):
    """The configuration for a role is incomplete or malformed."""


EXPLICIT_NONE = "explicit-none"


def normalize_effort(value: str | None) -> str | None:
    """Read an effort setting. ``none`` / ``off`` / ``unset`` / empty leave the effort out of the request (models
    without the knob); ``explicit-none`` sends the literal effort ``none`` (DeepSeek's no-thinking mode); any other
    value is sent as given."""
    if value is None:
        return None
    text = value.strip()
    if text.lower() in ("", "none", "off", "unset"):
        return None
    if text.lower() == EXPLICIT_NONE:
        return "none"
    return text


def parse_dotenv(path: Path) -> dict[str, str]:
    """Minimal dotenv reader: KEY=VALUE lines, # comments, optional quotes, optional 'export '."""
    values: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def environment(env_file: Path | None = None, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Process environment overlaid by an optional dotenv file (the file wins only for unset keys)."""
    merged = dict(os.environ if base is None else base)
    if env_file is not None:
        for key, value in parse_dotenv(env_file).items():
            merged.setdefault(key, value)
    return merged


def _get(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


@dataclass(frozen=True)
class RoleConfig:
    role: str
    base_url: str | None
    wire: str
    model: str | None
    effort: str | None
    api_key_var: str | None          # which variable actually supplied the key
    api_key: str | None = field(default=None, repr=False, compare=False)
    sources: dict = field(default_factory=dict, compare=False)

    def endpoint(self) -> str:
        """Full upstream URL for this role's wire."""
        if not self.base_url:
            raise ConfigError("AGENTSWE_%s_BASE_URL (or AGENTSWE_DEFAULT_BASE_URL) is not set" % self.role.upper())
        return endpoint_for(self.base_url, self.wire)

    def describe(self) -> dict:
        """Non-secret summary (safe to print, log or write to a manifest)."""
        host = urlsplit(self.base_url).hostname if self.base_url else None
        return {"role": self.role, "wire": self.wire, "model": self.model, "effort": self.effort,
                "base_url_host": host, "api_key_set": bool(self.api_key),
                "api_key_var": self.api_key_var, "sources": dict(self.sources)}


def endpoint_for(base_url: str, wire: str) -> str:
    """``https://h/v1`` + wire -> ``https://h/v1/responses`` | ``.../chat/completions``.

    A base URL that already names the route is used as is, so both
    ``https://api.deepseek.com/v1`` and ``https://api.deepseek.com/v1/responses`` work.
    """
    base = base_url.rstrip("/")
    route = {"responses": "/responses", "chat": "/chat/completions",
             "serper": "", "legacy-proxy": ""}.get(wire)
    if route is None:
        raise ConfigError("unknown wire %r" % (wire,))
    if not route or base.endswith(route):
        return base
    return base + route


def resolve(role: str, env: Mapping[str, str] | None = None, *, require_key: bool = True) -> RoleConfig:
    """Resolve one role from the environment, applying DEFAULT and Lite fallbacks."""
    role = role.lower()
    if role not in ROLES:
        raise ConfigError("unknown role %r (expected one of %s)" % (role, ", ".join(ROLES)))
    env = dict(os.environ) if env is None else env
    up = role.upper()
    sources: dict[str, str] = {}

    def pick(field_name: str, *, default_ok: bool = True) -> str | None:
        own = "AGENTSWE_%s_%s" % (up, field_name)
        value = _get(env, own)
        if value is not None:
            sources[field_name] = own
            return value
        if default_ok:
            shared = "AGENTSWE_DEFAULT_%s" % field_name
            value = _get(env, shared)
            if value is not None:
                sources[field_name] = shared
                return value
        return None

    if role == "search":
        base_url = pick("BASE_URL", default_ok=False)
        key = pick("API_KEY", default_ok=False)
        wire = pick("WIRE", default_ok=False) or DEFAULT_SEARCH_WIRE
        if wire not in SEARCH_WIRES:
            raise ConfigError("AGENTSWE_SEARCH_WIRE must be one of %s" % ", ".join(SEARCH_WIRES))
        if require_key and not key:
            raise ConfigError("AGENTSWE_SEARCH_API_KEY is not set")
        return RoleConfig(role, base_url, wire, None, None, sources.get("API_KEY"), key, sources)

    base_url = pick("BASE_URL")
    key = pick("API_KEY")
    wire = (pick("WIRE") or "responses").lower()
    if wire not in WIRES:
        raise ConfigError("AGENTSWE_%s_WIRE must be 'responses' or 'chat', got %r" % (up, wire))
    model = pick("MODEL", default_ok=False) or LITE_DEFAULTS[role]["model"]
    effort = normalize_effort(pick("EFFORT", default_ok=False) or LITE_DEFAULTS[role]["effort"])
    if require_key and not key:
        raise ConfigError("AGENTSWE_%s_API_KEY (or AGENTSWE_DEFAULT_API_KEY) is not set" % up)
    if base_url is not None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ConfigError("AGENTSWE_%s_BASE_URL must be an http(s) URL" % up)
        if parsed.username or parsed.password:
            raise ConfigError("AGENTSWE_%s_BASE_URL must not embed credentials" % up)
    return RoleConfig(role, base_url, wire, model, effort, sources.get("API_KEY"), key, sources)


def describe_all(env: Mapping[str, str] | None = None) -> list[dict]:
    """Doctor view: every role, never a secret value."""
    out = []
    for role in ROLES:
        try:
            out.append(resolve(role, env, require_key=False).describe())
        except ConfigError as exc:
            out.append({"role": role, "error": str(exc)})
    return out


def write_credential_file(path: Path, variable: str, value: str) -> Path:
    """Write one KEY=value line to a fresh 0600 file (for a read-only broker mount).

    The caller owns the file's lifetime; it must live under $AGENTSWE_HOME/tmp and
    be removed when the run ends.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("%s=%s\n" % (variable, value))
    return path


def read_credential(path: Path, variable: str) -> str:
    """Read exactly ``variable`` from a credential file; never fall back to another name."""
    value = parse_dotenv(Path(path)).get(variable, "").strip()
    if not value:
        raise ConfigError("credential file has no %s" % variable)
    return value
