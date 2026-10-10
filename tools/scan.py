#!/usr/bin/env python3
"""Pre-publication scan: identity / internal names, host paths, and credential-shaped strings.

Usage: tools/scan.py [ROOT] [--paths-from-git] [--release] [--max-show N]

* Identity and internal-name terms are stored only as SHA-256 hashes (tools/scan_terms.sha256),
  so the scanner itself publishes none of them. Text is lower-cased, split into alphanumeric
  tokens, and every 1-3 token window (space-joined) is hashed and looked up.
* Host paths (/home/<user>/, /Users/<user>/, /share/project) and e-mail addresses are regexes.
* Credential-shaped strings are reported masked, so the scan output never repeats a secret.
* Backups (*.pre-*, *.bak, *.orig, *.prev) and bytecode must not exist in the tree at all.
* Runtime references (rule runtime-ref:legacy): a field the Editing control plane requires to
  exist (case-contract and coverage-matrix entries, configuration-registry source files) must
  not name a @@AGENTSWE_LEGACY_*@@ path, because those render to directories a fresh install
  never has. Historical records may keep legacy paths; required inputs may not.

* As-run files (rule as-run-file): a task may ship files its as-run Builders saw although their names are otherwise
  forbidden (stale `*.pre-*` copies of task documents kept beside them in the paper trees). Each is listed in the
  task's `as_run_files.json` by exact repository path with its sha256; a listed file is scanned like any other text
  file, an unlisted one stays forbidden, and a listed file whose bytes differ from the pin, or that is missing,
  is a finding.
* CJK language (rules cjk / cjk-pending): release content is English. Explicitly allowlisted upstream language
  assets use `cjk`; all other CJK in Editing task content is `cjk-pending`. Pending content is reported as a
  release failure only with `--release`, so ordinary development scans can inspect other hygiene while conversion
  is in progress. Han characters written as escapes (`\\uXXXX`, `\\UXXXXXXXX`, `&#x...;`, `&#...;`) count as CJK
  too: a test once asserted an escaped Chinese sentence that the raw-character rule could not see.

Exit status 1 on any finding. Justified exceptions go in tools/scan_allow.txt as
`<path-glob> <rule>  # reason`.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF = {"tools/scan.py", "tools/scan_terms.sha256", "tools/scan_allow.txt"}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".agentswe"}
LITERAL_HOME = re.compile(r"""["']\$\{AGENTSWE_HOME\}[^"']*["']""")
FORBIDDEN_NAME = re.compile(r"(\.pre-|\.bak$|\.orig$|\.prev$|\.pyc$|^\.DS_Store$)")
TOKEN = re.compile(r"[a-z0-9]+")
PATTERNS = [
    ("path:home", re.compile(r"/home/[a-z][a-z0-9_-]*/"), False),
    ("path:macos-user", re.compile(r"/Users/[A-Za-z][A-Za-z0-9_-]*/"), False),
    ("path:shared-project", re.compile(r"/share/project/"), False),
    ("path:data-user", re.compile(r"/data/[a-z][a-z0-9_-]*/(?:opensource|lite-v1|0\d{3}-)"), False),
    ("ip:internal", re.compile(r"\b10\.(?:1|8)\.\d{1,3}\.\d{1,3}\b"), False),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@(?!example\.(?:com|org)\b)(?![A-Za-z0-9.-]+\.(?:invalid|test|example|localhost|local)\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), False),
    ("secret:openai-style", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"), True),
    ("secret:bearer", re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{32,}"), True),
    ("secret:aws", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), True),
    ("secret:private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), True),
    ("secret:github", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), True),
    ("secret:huggingface", re.compile(r"\bhf_[A-Za-z0-9]{30,}"), True),
    ("secret:dotenv", re.compile(
        r"(?m)^\s*[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)\s*=\s*['\"]?(?!\$\{)(?![a-z\-]*placeholder)(?![A-Z0-9_]+['\"]?\s*$)[A-Za-z0-9_\-\.]{24,}"), True),
]


def load_terms(root: Path) -> set[str]:
    path = root / "tools" / "scan_terms.sha256"
    return {line.split()[0] for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")}


def load_allow(root: Path) -> list[tuple[str, str]]:
    path = root / "tools" / "scan_allow.txt"
    rows = []
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                glob, rule = line.split()[:2]
                rows.append((glob, rule))
    return rows


def load_as_run_files(root: Path) -> tuple[dict[str, str], list[str]]:
    """Exact-path, sha-pinned exemptions from the forbidden-name rule, one list per task (tasks/*/*/as_run_files.json)."""
    pins: dict[str, str] = {}
    problems: list[str] = []
    for listing in sorted(root.glob("tasks/*/*/as_run_files.json")):
        task_dir = listing.parent.relative_to(root).as_posix() + "/"
        try:
            rows = json.loads(listing.read_text())["files"]
        except (ValueError, KeyError, TypeError):
            problems.append(f"{listing.relative_to(root).as_posix()}:0:as-run-file:unreadable-listing")
            continue
        for row in rows:
            rel, digest = row.get("path", ""), row.get("sha256", "")
            if not rel.startswith(task_dir) or ".." in rel.split("/") or len(digest) != 64:
                problems.append(f"{listing.relative_to(root).as_posix()}:0:as-run-file:bad-entry:{rel}")
                continue
            pins[rel] = digest
    return pins, problems


def files(root: Path, from_git: bool):
    if from_git:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
        for rel in out.decode().split("\0"):
            if rel:
                yield root / rel
        return
    for p in sorted(root.rglob("*")):
        if p.is_file() and not any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            yield p


def text_of(data: bytes) -> str | None:
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
HAN_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})|\\U([0-9a-fA-F]{8})|&#[xX]([0-9a-fA-F]{1,6});|&#([0-9]{1,7});")


def _escaped_han(match: re.Match) -> bool:
    hexa, hexb, hexc, dec = match.groups()
    code = int(hexa or hexb or hexc, 16) if (hexa or hexb or hexc) else int(dec)
    return code <= 0x10FFFF and bool(HAN.match(chr(code)))


def cjk_hits(text: str):
    """Yield one position per line containing Han characters, raw or written as an escape."""
    seen = set()
    positions = [m.start() for m in HAN.finditer(text)]
    positions += [m.start() for m in HAN_ESCAPE.finditer(text) if _escaped_han(m)]
    for pos in sorted(positions):
        line = text.count("\n", 0, pos) + 1
        if line not in seen:
            seen.add(line)
            yield pos


def term_hits(text: str, terms: set[str]):
    tokens = [(m.group(0), m.start()) for m in TOKEN.finditer(text.lower())]
    for i in range(len(tokens)):
        for n in (1, 2, 3):
            if i + n > len(tokens):
                break
            joined = " ".join(t for t, _ in tokens[i:i + n])
            if hashlib.sha256(joined.encode()).hexdigest() in terms:
                yield tokens[i][1], f"term:{hashlib.sha256(joined.encode()).hexdigest()[:10]}"


LEGACY = "@@AGENTSWE_LEGACY_"
RUNTIME_REF_FILES = ("tasks/editing/*/tree/meta/0905_case_contract.json",
                     "runners/editing/state/case_coverage_matrix.json",
                     "runners/editing/state/configuration_delta_registry.json")


def runtime_ref_findings(root: Path) -> list[tuple[str, str]]:
    """(relative path, offending field) for required-to-exist fields that name a legacy path."""
    import json
    out = []

    def walk(rel, value, trail):
        if isinstance(value, dict):
            for k, v in value.items():
                walk(rel, v, trail + [str(k)])
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(rel, v, trail + [str(i)])
        elif isinstance(value, str) and value.startswith(LEGACY):
            last = trail[-1] if trail else ""
            required = (last == "entry" or last.endswith("_entry")
                        or ("effective_source_files" in trail and last == "path"))
            if required:
                out.append((rel, "/".join(trail)))

    for pattern in RUNTIME_REF_FILES:
        for p in sorted(root.glob(pattern)):
            walk(p.relative_to(root).as_posix(), json.loads(p.read_text(encoding="utf-8")), [])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=str(ROOT))
    ap.add_argument("--paths-from-git", action="store_true")
    ap.add_argument("--release", action="store_true", help="fail on non-allowlisted CJK content")
    ap.add_argument("--max-show", type=int, default=40)
    a = ap.parse_args()
    root = Path(a.root).resolve()
    terms, allow = load_terms(root), load_allow(root)
    as_run, as_run_problems = load_as_run_files(root)

    def allowed(rel: str, rule: str) -> bool:
        return any(fnmatch.fnmatch(rel, g) and (rule == r or rule.startswith(r + ":") or r == "*") for g, r in allow)

    findings: list[str] = list(as_run_problems)
    seen_as_run: set[str] = set()
    ntext = nbin = 0
    for p in files(root, a.paths_from_git):
        rel = p.relative_to(root).as_posix()
        if rel in SELF:
            continue
        if FORBIDDEN_NAME.search(p.name):
            if rel not in as_run:
                findings.append(f"{rel}:0:forbidden-file")
                continue
            seen_as_run.add(rel)
            if hashlib.sha256(p.read_bytes()).hexdigest() != as_run[rel]:
                findings.append(f"{rel}:0:as-run-file:sha-mismatch")
                continue
        try:
            text = text_of(p.read_bytes())
        except OSError:
            continue
        if text is None:
            nbin += 1
            continue
        ntext += 1
        # Avoid interpreting embedded PDF, office, image, archive, and database bytes as source text.
        binary_suffixes = {".pdf", ".pptx", ".docx", ".xlsx", ".png", ".jpg", ".jpeg", ".gif", ".tar", ".gz", ".enc", ".sqlite"}
        for pos in cjk_hits(text) if p.suffix.lower() not in binary_suffixes else ():
            line = text.count("\n", 0, pos) + 1
            # `cjk` is a permanent, path-scoped exception for upstream assets that are intentionally
            # multilingual. Converted task content remains pending until a translator replaces it.
            if a.release and not allowed(rel, "cjk") and not allowed(rel, "cjk-pending"):
                findings.append(f"{rel}:{line}:cjk-pending")
        hits = [(pos, name, None if secret else m.group(0)[:60])
                for name, rx, secret in PATTERNS for m in rx.finditer(text) for pos in [m.start()]]
        hits += [(pos, name, None) for pos, name in term_hits(text, terms)]
        if rel.endswith(".py"):
            # scrub artefact: Python does not expand "${AGENTSWE_HOME}"; such a path only works as the default of
            # an environment variable the runner always sets (allow-list those with the reason)
            hits += [(m.start(), "scrub:literal-home-in-python", m.group(0)[:60])
                     for m in LITERAL_HOME.finditer(text)]
        for pos, name, shown in hits:
            if not allowed(rel, name):
                line = text.count("\n", 0, pos) + 1
                findings.append(f"{rel}:{line}:{name}" + (f":{shown}" if shown else ""))
    findings += [f"{rel}:0:as-run-file:missing" for rel in sorted(set(as_run) - seen_as_run)]
    for rel, field in runtime_ref_findings(root):
        if not allowed(rel, "runtime-ref:legacy"):
            findings.append(f"{rel}:0:runtime-ref:legacy:{field}")
    print(f"scanned {ntext} text files ({nbin} binary files not text-scanned) under {root}")
    if findings:
        print(f"FAIL: {len(findings)} findings")
        for f in findings[: a.max_show]:
            print("  " + f)
        if len(findings) > a.max_show:
            print(f"  ... {len(findings) - a.max_show} more")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
