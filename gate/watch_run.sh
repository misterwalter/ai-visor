#!/usr/bin/env bash
# Print one status line a minute for a gate run until its log says it is done.
# Usage: watch_run.sh <run log>
LOG="${1:?run log required}"
RUN="$(grep -m1 -oP '(?<=\] run )\S+' "$LOG")"
[ -n "$RUN" ] || { echo "no run named in $LOG" >&2; exit 1; }
while ! grep -q '^GATE_RUN_DONE' "$LOG"; do
  calls=$(jq -c 'select(.status == 200)' "/srv/code/gate-results/$RUN/model-calls.jsonl" 2>/dev/null | wc -l)
  changed=$(git -C "/srv/code/work/$RUN" status --short 2>/dev/null | wc -l)
  swap=$(free -m | awk '/^Swap/{print $3}')
  echo "STATUS $(date +%H:%M) calls=$calls changed_files=$changed swap_mb=$swap"
  sleep 60
done
tail -4 "$LOG"
echo "WATCH_FINISHED"
