"""Configuration: `.env` file plus process environment, resolved per model role.

Precedence: process environment > `.env` (repository root, or --env-file) > profile file > built-in defaults.
Roles: BUILDER (the coding agent under evaluation), RUNTIME (the model a candidate agent calls,
only through the evaluator broker), JUDGE (Result judge, user simulators, LLM graders), SEARCH.
A role setting is looked up as AGENTSWE_<FAMILY>_<ROLE>_<KEY> (when the caller names a family: CREATION,
EDITING, OPTIMIZATION), then AGENTSWE_<ROLE>_<KEY>, then AGENTSWE_DEFAULT_<KEY>.
Profiles (AGENTSWE_PROFILE): `paper` (the default when unset) or `lite-v1.1` selects the Builder-visible bytes of
every task (tasks/<family>/<id>/profiles/<profile>/ overlays). Setting AGENTSWE_PROFILE explicitly also loads
profiles/<profile>.env, the role models and efforts of that configuration, below `.env`; left unset, the built-in
smoke defaults (deepseek-flash for every role) apply with the paper bytes; among them the Creation runtime runs
without reasoning (SMOKE_CREATION_RUNTIME_EFFORT), see `smoke_creation_runtime_effort`, and so does the OSWorld
vision broker (SMOKE_OSWORLD_EFFORT), see `smoke_osworld_effort`.
Key values are never printed; `describe()` reports only whether each key is set.
"""
from __future__ import annotations

import os
import subprocess
import shlex
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ROLES = ("BUILDER", "RUNTIME", "JUDGE")
FAMILIES = ("CREATION", "EDITING", "OPTIMIZATION")
PROFILES = ("paper", "lite-v1.1")
LITE_DEFAULTS = {
    "AGENTSWE_DEFAULT_BASE_URL": "https://api.deepseek.com/v1",
    "AGENTSWE_DEFAULT_WIRE": "responses",
    "AGENTSWE_BUILDER_MODEL": "deepseek-flash", "AGENTSWE_BUILDER_EFFORT": "max",
    "AGENTSWE_RUNTIME_MODEL": "deepseek-flash", "AGENTSWE_RUNTIME_EFFORT": "high",
    "AGENTSWE_JUDGE_MODEL": "deepseek-flash", "AGENTSWE_JUDGE_EFFORT": "max",
    "AGENTSWE_NETWORK_POOL": "10.246.0.0/16",
    "AGENTSWE_MAX_CONCURRENT_RUNS": "4",
    "AGENTSWE_CONDA_CHANNEL": "https://conda.anaconda.org/conda-forge",
    "AGENTSWE_PIP_INDEX_URL": "https://pypi.org/simple",
    "AGENTSWE_NPM_REGISTRY": "https://registry.npmjs.org",
    "AGENTSWE_NODE_DIST_URL": "https://nodejs.org/dist",
    "AGENTSWE_UBUNTU_MIRROR": "http://archive.ubuntu.com/ubuntu",
    "AGENTSWE_RELEASE_ASSETS_URL": "https://github.com/VectorSpaceLab/AgentSWE/releases/download/assets-v1",
    # OSWorld vision broker: strict structured output off for the default DeepSeek endpoint, which rejects
    # the action schema's nullable type unions in strict mode (the paper profile sets it on).
    "AGENTSWE_OSWORLD_SCHEMA_STRICT": "0",
}

# Smoke default only (no profile, built-in Creation runtime): the Creation runtime runs deepseek-flash without
# reasoning. Candidates written for the paper runtime set output caps of ~1-9k tokens, and flash at effort high spends
# them on reasoning (GUI, Web, PPTX smokes); effort low/medium/minimal kept 3-6k reasoning tokens on the Web planner
# request, `none` 0. Profiles keep their configured effort; the paper's Lite Creation runs used high.
SMOKE_CREATION_RUNTIME_EFFORT = "explicit-none"
SMOKE_RUNTIME_OVERRIDES = ("AGENTSWE_CREATION_RUNTIME_EFFORT", "AGENTSWE_RUNTIME_EFFORT",
                           "AGENTSWE_CREATION_RUNTIME_MODEL", "AGENTSWE_RUNTIME_MODEL",
                           "AGENTSWE_CREATION_RUNTIME_BASE_URL", "AGENTSWE_RUNTIME_BASE_URL", "AGENTSWE_DEFAULT_BASE_URL")


# Smoke default only (no profile, built-in Optimization runtime provider and model): the OSWorld vision broker
# (AGENTSWE_OSWORLD_EFFORT, otherwise the paper's `high`) runs deepseek-flash without reasoning. Replaying recorded
# action requests at effort high spent 0.8-4.1k reasoning tokens per action (3.1-6.2k tokens per call), and both
# json_object smoke rolls ended at the 100,000-token episode budget after 12-26 calls; at `none` a call took 2.1-2.9k
# tokens with 0 reasoning tokens (2026-10-05). Profiles keep the broker's `high`.
SMOKE_OSWORLD_EFFORT = "explicit-none"
SMOKE_OSWORLD_OVERRIDES = ("AGENTSWE_OSWORLD_EFFORT",
                           "AGENTSWE_OPTIMIZATION_RUNTIME_MODEL", "AGENTSWE_RUNTIME_MODEL",
                           "AGENTSWE_OPTIMIZATION_RUNTIME_BASE_URL", "AGENTSWE_RUNTIME_BASE_URL",
                           "AGENTSWE_DEFAULT_BASE_URL")


def smoke_creation_runtime_effort(profile: str | None, user: dict[str, str]) -> str | None:
    """The Creation runtime effort the smoke defaults add, or None (a profile is set, or the user configured the
    Creation runtime's model, provider or effort)."""
    if profile or any(user.get(k) for k in SMOKE_RUNTIME_OVERRIDES):
        return None
    return SMOKE_CREATION_RUNTIME_EFFORT


def smoke_osworld_effort(profile: str | None, user: dict[str, str]) -> str | None:
    """The OSWorld vision-broker effort the smoke defaults add, or None (a profile is set, or the user configured
    AGENTSWE_OSWORLD_EFFORT or the Optimization runtime's model or provider). Same rule as the Creation runtime."""
    if profile or any(user.get(k) for k in SMOKE_OSWORLD_OVERRIDES):
        return None
    return SMOKE_OSWORLD_EFFORT


def parse_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] in {"'", '"'}:
            try:
                value = shlex.split(value)[0] if value else ""
            except ValueError:
                value = value.strip("'\"")
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    return values


def resolve_file_reference(value: str) -> str:
    """`@file:/path/to/dotenv#VAR` reads VAR from an existing credential file instead of copying the key."""
    ref = value[len("@file:"):]
    path, _, var = ref.partition("#")
    data = parse_dotenv(Path(path).expanduser())
    if not var or var not in data:
        raise SystemExit(f"credential reference {path}#{var or '?'} does not resolve")
    return data[var]


@dataclass
class Role:
    name: str
    base_url: str
    api_key: str = field(repr=False)
    wire: str
    model: str
    effort: str

    @property
    def responses_url(self) -> str:
        return self.base_url.rstrip("/") + "/responses"

    def describe(self) -> dict:
        return {"base_url": self.base_url, "api_key": "set" if self.api_key else "MISSING",
                "wire": self.wire, "model": self.model, "effort": self.effort}


@dataclass
class Config:
    values: dict[str, str]
    env_file: Path | None

    def get(self, key: str, default: str | None = None) -> str | None:
        v = self.values.get(key)
        if v and v.startswith("@file:"):
            v = resolve_file_reference(v)
        return v if v not in (None, "") else default

    @property
    def profile(self) -> str:
        value = self.get("AGENTSWE_PROFILE") or "paper"
        if value not in PROFILES:
            raise SystemExit(f"AGENTSWE_PROFILE={value!r}: expected one of {', '.join(PROFILES)}")
        return value

    def role(self, name: str, family: str | None = None) -> Role:
        name = name.upper()
        fam = family.upper() if family else None

        def g(k: str) -> str:
            return ((fam and self.get(f"AGENTSWE_{fam}_{name}_{k}")) or self.get(f"AGENTSWE_{name}_{k}")
                    or self.get(f"AGENTSWE_DEFAULT_{k}") or "")
        return Role(name=name, base_url=g("BASE_URL"), api_key=g("API_KEY"), wire=(g("WIRE") or "responses").lower(),
                    model=g("MODEL"), effort=g("EFFORT"))

    @property
    def search(self) -> tuple[str, str]:
        return self.get("AGENTSWE_SEARCH_BASE_URL", "") or "", self.get("AGENTSWE_SEARCH_API_KEY", "") or ""

    @property
    def home(self) -> Path:
        # Never inside the repository checkout (see home_git_toplevel): the default is the XDG data directory.
        default = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "agentswe"
        return Path(self.get("AGENTSWE_HOME") or default).expanduser().resolve()

    def home_git_toplevel(self) -> str | None:
        """The git work tree AGENTSWE_HOME lies in, if any.

        Evaluators run git (apply, diff, status) in run directories and task trees under AGENTSWE_HOME that are
        not repositories themselves. Inside an enclosing work tree, git resolves paths against that repository
        instead: `git apply` then silently skips every hunk outside the current directory and exits 0, so a
        Candidate's patch is never applied. AGENTSWE_HOME must therefore not be inside any git work tree.
        """
        probe = self.home
        while not probe.exists():
            probe = probe.parent
        done = subprocess.run(["git", "-C", str(probe), "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, check=False)
        return (done.stdout.strip() or None) if done.returncode == 0 else None

    def require_home_outside_git(self) -> None:
        top = self.home_git_toplevel()
        if top:
            raise SystemExit(f"AGENTSWE_HOME ({self.home}) is inside the git work tree {top}; set AGENTSWE_HOME to a "
                             f"directory outside any git repository (evaluators run git there and would resolve paths "
                             f"against {top})")

    def describe(self) -> dict:
        out = {r: self.role(r).describe() for r in ROLES}
        out["PROFILE"] = {"name": self.profile, "explicit": bool(self.get("AGENTSWE_PROFILE")),
                          "families": {f: {r: self.role(r, f).describe() for r in ("RUNTIME", "JUDGE")}
                                       for f in FAMILIES}}
        base, key = self.search
        out["SEARCH"] = {"base_url": base or "(not configured)", "api_key": "set" if key else "not set"}
        out["HOST"] = {k: self.get(k) for k in ("AGENTSWE_HOME", "AGENTSWE_NETWORK_POOL", "AGENTSWE_MAX_CONCURRENT_RUNS",
                                                 "AGENTSWE_BROKER_HOST")}
        out["HOST"]["AGENTSWE_HOME"] = str(self.home)
        out["env_file"] = str(self.env_file) if self.env_file else None
        return out


def load(env_file: str | Path | None = None) -> Config:
    path = Path(env_file).expanduser().resolve() if env_file else REPO_ROOT / ".env"
    dotenv = parse_dotenv(path)
    process = {k: v for k, v in os.environ.items() if k.startswith("AGENTSWE_")}
    values = dict(LITE_DEFAULTS)
    profile = process.get("AGENTSWE_PROFILE") or dotenv.get("AGENTSWE_PROFILE")
    if profile:
        profile_file = REPO_ROOT / "profiles" / f"{profile}.env"
        if not profile_file.is_file():
            raise SystemExit(f"AGENTSWE_PROFILE={profile!r}: no {profile_file.relative_to(REPO_ROOT)}")
        values.update(parse_dotenv(profile_file))
    values.update(dotenv)
    values.update(process)
    smoke_effort = smoke_creation_runtime_effort(profile, {**dotenv, **process})
    if smoke_effort:
        values["AGENTSWE_CREATION_RUNTIME_EFFORT"] = smoke_effort
    osworld_effort = smoke_osworld_effort(profile, {**dotenv, **process})
    if osworld_effort:
        values["AGENTSWE_OSWORLD_EFFORT"] = osworld_effort
    return Config(values=values, env_file=path if path.is_file() else None)
