#!/usr/bin/python3
"""Run native Codex with two explicit feature overrides, preserving argv."""
import os
import sys


DISABLED_FEATURES = frozenset(("unified_exec", "code_mode_host"))
NATIVE_CODEX = "/opt/openai/codex/bin/codex.js"


def native_args(args):
    """Remove only conflicting feature enables and preserve all other argv."""
    out = ["--disable", "unified_exec", "--disable", "code_mode_host"]
    value_options = {
        "-c", "--config", "-m", "--model", "-p", "--profile",
        "-C", "--cd", "-s", "--sandbox", "-i", "--image",
        "-o", "--output-last-message", "--output-schema", "--disable",
        "--color", "--add-dir", "--local-provider",
    }
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            out.extend(args[index:])
            break
        if arg == "--enable" and index + 1 < len(args):
            feature = args[index + 1]
            if feature not in DISABLED_FEATURES:
                out.extend((arg, feature))
            index += 2
            continue
        if arg.startswith("--enable=") and arg.split("=", 1)[1] in DISABLED_FEATURES:
            index += 1
            continue
        # An option value may itself look like a feature flag. Preserve the
        # option/value pair before interpreting the next argv token.
        if arg in value_options and index + 1 < len(args):
            out.extend(args[index:index + 2])
            index += 2
            continue
        out.append(arg)
        index += 1
    return out


if __name__ == "__main__":
    os.execv(NATIVE_CODEX, ["codex"] + native_args(sys.argv[1:]))
