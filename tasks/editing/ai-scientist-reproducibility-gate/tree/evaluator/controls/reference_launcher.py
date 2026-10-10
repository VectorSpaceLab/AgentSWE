import sys

from ai_scientist.claim_verification import main as _claim_main

# Production governed route: --verify-claims-only accepts --claim-project-id and --claim-budget-store.
verify_claims_only = True


def parse_arguments(argv=None):
    return argv if argv is not None else sys.argv[1:]


def main():
    argv = parse_arguments()
    if "--verify-claims-only" not in argv:
        return 0
    translated = []
    for value in argv:
        translated.append("--" + value[8:] if value.startswith("--claim-") else value)
    translated.remove("--verify-claims-only")
    return _claim_main(translated)


if __name__ == "__main__":
    raise SystemExit(main())
