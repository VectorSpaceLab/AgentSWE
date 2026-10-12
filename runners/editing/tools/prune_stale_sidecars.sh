#!/bin/bash
# Remove leaked harbor egress-control sidecars (+ their compose networks) left by Edit runs that
# ended before today. Live-run resources (created today) are never touched. Watchers are paused
# so no admit/finalize inventory window can overlap the batch.
MODE=${1:-dry}
echo "mode=$MODE $(date +%T)"
pids=$(pgrep -f "wait_admit" | tr "\n" " ")
[ "$MODE" = apply ] && [ -n "$pids" ] && kill -STOP $pids && echo "paused watchers: $pids"
n=$(pgrep -af "admit.py|readiness_finalize" | grep -vc pgrep); echo "admit processes live: $n"
if [ "$n" != 0 ] && [ "$MODE" = apply ]; then echo "REFUSING: admit in flight"; kill -CONT $pids; exit 1; fi
removed=0; kept=0
for net in $(docker network ls --format "{{.Name}}" | grep -E "__env_default$"); do
  created=$(docker network inspect $net --format "{{.Created}}" | cut -c1-10)
  cs=$(docker network inspect $net --format "{{range .Containers}}{{.Name}} {{end}}")
  wd=""; for c in $cs; do wd="$wd $(docker inspect $c --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')"; done
  if [ "$created" \< "2026-09-21" ] && echo "$wd" | grep -qE "@@AGENTSWE_LEGACY_DATA@@/0905-edit-|@@AGENTSWE_LEGACY_DATA@@/0915-edit" && ! echo "$wd" | grep -qiE "create"; then
    echo "REMOVE $net created=$created wd=$wd containers=$cs"
    if [ "$MODE" = apply ]; then
      for c in $cs; do docker rm -f $c >/dev/null && echo "  rm container $c"; done
      docker network rm $net >/dev/null && echo "  rm network $net"
    fi
    removed=$((removed+1))
  else
    echo "KEEP   $net created=$created wd=$wd"; kept=$((kept+1))
  fi
done
echo "removed=$removed kept=$kept"
[ "$MODE" = apply ] && [ -n "$pids" ] && kill -CONT $pids && echo "resumed watchers"
echo "networks now: $(docker network ls -q | wc -l)  $(date +%T)"
