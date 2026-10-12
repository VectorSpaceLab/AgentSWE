#!/usr/bin/env bash
# Publish the flat release-asset upload set (tools/release_assets.py build) as the assets of one GitHub release.
#
#   tools/publish_release_assets.sh <owner/repo> <tag> <flat dir>
#   e.g. tools/publish_release_assets.sh <owner>/<repo> assets-v1 ./agentswe-assets-v1
#
# Needs bash (3.2 or later), curl and python3 (to read JSON), on macOS or Linux, and only the flat directory, not a
# checkout. Credentials, in this order:
#   GITHUB_TOKEN  from the environment: a fine-grained token with "Contents: Read and write" on the repository, or a
#                 classic token with the `repo` scope (`public_repo` is enough for a public repository);
#   gh            otherwise the token of an authenticated GitHub CLI (`gh auth login`; its default scopes include
#                 `repo`), read with `gh auth token`.
# The token is never printed and never put on a command line: each curl reads its Authorization header as a config
# line on standard input, written by the shell's builtin printf. curl uses HTTPS_PROXY / https_proxy when set.
#
# 1. checks the directory against its manifest: exactly the listed files, their sizes, and SHA256SUMS (set
#    PUBLISH_SKIP_SHA=1 to check names and sizes only);
# 2. finds the release, or creates it (the tag is created on the default branch if it does not exist; the notes are
#    the set's README.md);
# 3. uploads each file, skipping an asset already present with the same size; an asset with another size, or left
#    unfinished by an interrupted upload, is deleted and uploaded again; each upload is tried PUBLISH_ATTEMPTS times
#    (default 5);
# 4. lists the release's assets and compares names, sizes and states with the manifest (exit 1 on any difference),
#    then downloads SHA256SUMS through its public URL as a check of the download path.
# Re-running resumes: assets already complete are skipped.
set -euo pipefail

die() { echo "publish: $*" >&2; exit 1; }
log() { echo "publish: $*" >&2; }

[ $# -eq 3 ] || die "usage: $0 <owner/repo> <tag> <flat dir>"
REPO=$1
TAG=$2
DIR=${3%/}
case $REPO in */*) ;; *) die "expected <owner/repo>, got $REPO" ;; esac
API=${GITHUB_API_URL:-https://api.github.com}
API=${API%/}
ATTEMPTS=${PUBLISH_ATTEMPTS:-5}
DELAY=${PUBLISH_RETRY_DELAY:-20}
MANIFEST=release-assets-manifest.json
TAB=$(printf '\t')

command -v curl >/dev/null 2>&1 || die "curl not found"
command -v python3 >/dev/null 2>&1 || die "python3 not found (it reads the manifest and the API's JSON)"
[ -f "$DIR/$MANIFEST" ] || die "$DIR/$MANIFEST not found; build the set with tools/release_assets.py build"

WORK=$(mktemp -d "${TMPDIR:-/tmp}/publish-assets.XXXXXX")
trap 'rm -rf "$WORK"' EXIT

cat > "$WORK/publish_json.py" <<'EOF'
import json, os, sys
cmd, args = sys.argv[1], sys.argv[2:]
if cmd == "expected":  # manifest -> name, size, sha256 of every asset, the manifest itself last
    for a in json.load(open(args[0]))["assets"]:
        print("%s\t%d\t%s" % (a["name"], a["size"], a["sha256"]))
    print("%s\t%d\t-" % (os.path.basename(args[0]), os.path.getsize(args[0])))
elif cmd == "create":  # tag, README -> the create-release request
    print(json.dumps({"tag_name": args[0], "name": "AgentSWE release assets (%s)" % args[0],
                      "body": open(args[1]).read(), "draft": False, "prerelease": False}))
elif cmd == "release":  # release -> id, upload URL
    r = json.load(open(args[0]))
    print("%s\t%s" % (r["id"], r["upload_url"].split("{")[0]))
elif cmd == "page":  # one page of assets, appended to a TSV -> how many it held
    rows = json.load(open(args[0]))
    with open(args[1], "a") as out:
        for a in rows:
            out.write("%s\t%d\t%s\t%s\t%s\n" % (a["name"], a["size"], a["state"], a["id"], a["browser_download_url"]))
    print(len(rows))
elif cmd == "message":  # the API's error message in a response body
    try:
        d = json.load(open(args[0]))
        print(d.get("message", "") + "".join(" [%s]" % e.get("code", e) for e in d.get("errors", []) if isinstance(e, dict)))
    except Exception:
        pass
elif cmd == "compare":  # expected TSV, release assets TSV -> exit 1 on any difference
    want = dict((l.split("\t")[0], int(l.split("\t")[1])) for l in open(args[0]))
    have = dict((l.split("\t")[0], (int(l.split("\t")[1]), l.split("\t")[2])) for l in open(args[1]))
    problems = ["missing: %s" % n for n in sorted(set(want) - set(have))]
    problems += ["not in the manifest: %s" % n for n in sorted(set(have) - set(want))]
    for n in sorted(set(want) & set(have)):
        if have[n] != (want[n], "uploaded"):
            problems.append("%s: %d bytes (%s), the manifest says %d" % (n, have[n][0], have[n][1], want[n]))
    for p in problems:
        sys.stderr.write("publish:   %s\n" % p)
    sys.stderr.write("publish: the release has %d assets, %d bytes; the manifest lists %d: %s\n" % (
        len(have), sum(s for s, _ in have.values()), len(want), "%d differences" % len(problems) if problems else "match"))
    sys.exit(1 if problems else 0)
EOF
js() { python3 "$WORK/publish_json.py" "$@"; }

TOKEN=${GITHUB_TOKEN:-}
SOURCE=GITHUB_TOKEN
if [ -z "$TOKEN" ] && command -v gh >/dev/null 2>&1; then
  TOKEN=$(gh auth token 2>/dev/null || true)
  SOURCE="gh auth token"
fi
[ -n "$TOKEN" ] || die "no credentials: set GITHUB_TOKEN, or log in with gh auth login"

# api <method> <url> <response body file> [curl args]: prints the HTTP status (000 when curl itself failed)
api() {
  local method=$1 url=$2 out=$3 code
  shift 3
  rm -f "$out"
  code=$(printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" |
    curl -sS --config - -X "$method" -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" --connect-timeout 30 -o "$out" -w '%{http_code}' "$@" "$url") || code=000
  echo "$code"
}

size_of() { wc -c < "$1" | tr -d ' '; }

sha256() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$@"; else sha256sum "$@"; fi
}

# --- 1. the local set ---------------------------------------------------------------------------------------------
js expected "$DIR/$MANIFEST" > "$WORK/expected.tsv"
cut -f1 "$WORK/expected.tsv" | LC_ALL=C sort > "$WORK/expected.names"
(cd "$DIR" && ls -A) | LC_ALL=C sort > "$WORK/local.names"
if ! cmp -s "$WORK/expected.names" "$WORK/local.names"; then
  diff "$WORK/expected.names" "$WORK/local.names" | sed 's/^</  missing:/; s/^>/  not in the manifest:/' | grep '^  ' >&2 || true
  die "$DIR is not exactly the upload set its manifest lists"
fi
COUNT=0
TOTAL=0
while IFS=$TAB read -r name size sha <&3; do
  case $name in *[!A-Za-z0-9._-]*) die "$name: not a plain asset name" ;; esac
  actual=$(size_of "$DIR/$name")
  [ "$actual" = "$size" ] || die "$name: $actual bytes, the manifest says $size"
  COUNT=$((COUNT + 1))
  TOTAL=$((TOTAL + size))
done 3< "$WORK/expected.tsv"
if [ "${PUBLISH_SKIP_SHA:-0}" != 1 ]; then
  log "checking SHA256SUMS ($TOTAL bytes)"
  (cd "$DIR" && sha256 -c SHA256SUMS) > "$WORK/sums.out" 2>&1 || {
    grep -v ': OK$' "$WORK/sums.out" >&2
    die "SHA256SUMS check failed"
  }
  want=$(awk -F"$TAB" '$1 == "SHA256SUMS" {print $3}' "$WORK/expected.tsv")
  [ "$(sha256 "$DIR/SHA256SUMS" | cut -d' ' -f1)" = "$want" ] || die "SHA256SUMS differs from its manifest entry"
fi
log "$DIR: $COUNT files, $TOTAL bytes, as the manifest lists (credentials: $SOURCE)"

# --- 2. the release -----------------------------------------------------------------------------------------------
code=$(api GET "$API/repos/$REPO/releases/tags/$TAG" "$WORK/release.json")
case $code in
  200) log "release $TAG exists" ;;
  404)
    log "creating release $TAG in $REPO"
    js create "$TAG" "$DIR/README.md" > "$WORK/create.json"
    code=$(api POST "$API/repos/$REPO/releases" "$WORK/release.json" -H "Content-Type: application/json" \
      --data-binary @"$WORK/create.json")
    [ "$code" = 201 ] || die "creating the release failed (HTTP $code) $(js message "$WORK/release.json")"
    ;;
  401|403) die "HTTP $code reading the releases of $REPO: the token is invalid or lacks access $(js message "$WORK/release.json")" ;;
  *) die "HTTP $code reading release $TAG of $REPO $(js message "$WORK/release.json")" ;;
esac
js release "$WORK/release.json" > "$WORK/release.tsv"
IFS=$TAB read -r RID UPLOAD < "$WORK/release.tsv"

list_assets() {  # -> $WORK/assets.tsv: name, size, state, id, download URL of every asset of the release
  local page=1 code n
  : > "$WORK/assets.tsv"
  while :; do
    code=$(api GET "$API/repos/$REPO/releases/$RID/assets?per_page=100&page=$page" "$WORK/page.json")
    [ "$code" = 200 ] || { log "listing the assets failed (HTTP $code) $(js message "$WORK/page.json")"; return 1; }
    n=$(js page "$WORK/page.json" "$WORK/assets.tsv") || return 1
    case $n in '' | *[!0-9]*) return 1 ;; esac
    [ "$n" -eq 0 ] && return 0  # pages until an empty one, whatever page size the server applies
    page=$((page + 1))
  done
}

remote() { awk -F"$TAB" -v n="$1" '$1 == n' "$WORK/assets.tsv"; }  # the release's asset row of a name, if any
field() { echo "$1" | cut -f"$2"; }

delete_asset() {
  local code
  code=$(api DELETE "$API/repos/$REPO/releases/assets/$1" "$WORK/delete.json")
  [ "$code" = 204 ] || [ "$code" = 404 ] || { log "deleting asset $1 failed (HTTP $code)"; return 1; }
}

upload() {  # upload <name> <size>
  local name=$1 size=$2 attempt=1 code row
  while [ "$attempt" -le "$ATTEMPTS" ]; do
    log "uploading $name ($size bytes), attempt $attempt of $ATTEMPTS"
    code=$(api POST "$UPLOAD?name=$name" "$WORK/upload.json" -H "Content-Type: application/octet-stream" \
      -H "Expect:" -T "$DIR/$name" --speed-limit 1024 --speed-time 120)
    [ "$code" = 201 ] && return 0
    log "$name: HTTP $code $(js message "$WORK/upload.json")"
    # a failed upload can leave the asset behind: unfinished, or complete with the response lost
    if list_assets; then
      row=$(remote "$name")
      if [ -n "$row" ]; then
        [ "$(field "$row" 2)" = "$size" ] && [ "$(field "$row" 3)" = uploaded ] && return 0
        delete_asset "$(field "$row" 4)" || true
      fi
    fi
    [ "$attempt" -lt "$ATTEMPTS" ] && sleep $((attempt * DELAY))
    attempt=$((attempt + 1))
  done
  return 1
}

# --- 3. uploads ---------------------------------------------------------------------------------------------------
list_assets || die "cannot list the assets of release $TAG"
UPLOADED=0
SKIPPED=0
FAILED=""
while IFS=$TAB read -r name size sha <&3; do
  row=$(remote "$name")
  if [ -n "$row" ]; then
    if [ "$(field "$row" 2)" = "$size" ] && [ "$(field "$row" 3)" = uploaded ]; then
      SKIPPED=$((SKIPPED + 1))
      continue
    fi
    log "$name: present with $(field "$row" 2) bytes ($(field "$row" 3)); replacing it"
    delete_asset "$(field "$row" 4)" || die "cannot replace $name"
  fi
  if upload "$name" "$size"; then
    UPLOADED=$((UPLOADED + 1))
  else
    FAILED="$FAILED $name"
  fi
done 3< "$WORK/expected.tsv"
log "uploaded $UPLOADED, already present $SKIPPED${FAILED:+, failed:$FAILED}"

# --- 4. verification ----------------------------------------------------------------------------------------------
list_assets || die "cannot list the assets of release $TAG"
js compare "$WORK/expected.tsv" "$WORK/assets.tsv" || die "release $TAG does not match the manifest; re-run to resume"
url=$(field "$(remote SHA256SUMS)" 5)
if curl -fsSL --connect-timeout 30 -o "$WORK/SHA256SUMS.public" "$url" && cmp -s "$WORK/SHA256SUMS.public" "$DIR/SHA256SUMS"; then
  log "public download of SHA256SUMS matches ($url)"
else
  log "warning: the public download of SHA256SUMS failed or differs (a private repository?): $url"
fi
log "done; setup reads this release with AGENTSWE_RELEASE_ASSETS_URL=${url%/SHA256SUMS}"
