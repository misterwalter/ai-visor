#!/usr/bin/env bash
# One gate run: fresh clone -> branch -> baseline tests -> agent, inside the wall
# -> tests -> report.
#
#   run_gate.sh <repo> <task.md> <model> <harness> [--plan-only] [--notes <file>]
#
# <repo>        a local clone of the project to work on; each run copies it afresh
# <harness>     the agent loop to use: qwen or pi (see harness/)
# --plan-only   the agent may read but not edit; its plan and questions are the output
# --notes FILE  the owner's replies from an earlier plan round, appended to the prompt
set -u

REPO_SRC="$(realpath "${1:?repo required}")"; TASK="$(realpath "${2:?task file required}")"
MODEL="${3:?model required}"; HARNESS="${4:?harness required: qwen or pi}"; shift 4
PLAN_ONLY=0; NOTES=""
while [ $# -gt 0 ]; do
  case "$1" in
    --plan-only) PLAN_ONLY=1 ;;
    --notes) NOTES="$(realpath "$2")"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

HERE="$(dirname "$(realpath "$0")")"
[ -f "$HERE/harness/$HARNESS.sh" ] || { echo "unknown harness: $HARNESS" >&2; exit 2; }
export PATH="$HERE/bin:$HOME/.npm-global/bin:$PATH"
# Qwen Code gives up on any single reply after 15 minutes unless told otherwise,
# and on a CPU a long reply takes longer than that. No setting in its file covers this.
export QWEN_STREAM_MAX_LIFETIME_MS=0
# pi: no version check, no usage report, no catalogue refresh. None could leave
# the wall anyway; this saves it trying.
export PI_OFFLINE=1 PI_TELEMETRY=0 PI_SKIP_VERSION_CHECK=1
NAME="$(basename "$TASK" .md)"
RUN="$NAME-$MODEL-$HARNESS-$(date +%Y%m%d-%H%M)"
WORK="/srv/code/work/$RUN"
OUT="/srv/code/gate-results/$RUN"
WALL="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/gate-$RUN"
CALLS="$OUT/model-calls.jsonl"
MODEL_SERVER="127.0.0.1:11434"
mkdir -p "$OUT/harness-log" /srv/code/work

say() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$OUT/run.log"; }

say "run $RUN  (plan_only=$PLAN_ONLY)"

# shellcheck source=/dev/null
. "$HERE/harness/$HARNESS.sh"

for tool in bwrap socat python3 jq flock git flatpak "$HARNESS"; do
  command -v "$tool" > /dev/null || { say "missing program: $tool"; exit 1; }
done
# The test door hands these paths to socat, which splits its arguments on spaces and commas.
for path in "$WORK" "$WALL"; do
  [[ "$path" =~ ^[A-Za-z0-9_./-]+$ ]] \
    || { say "only letters, digits, dot, dash and underscore may appear in: $path"; exit 1; }
done
# The system allows a socket's path 107 characters, and the doors' names take 11.
[ "${#WALL}" -le 90 ] || { say "the task name is too long for a socket path: $WALL"; exit 1; }

git -C "$REPO_SRC" pull --quiet --ff-only origin main || say "WARNING: could not update $REPO_SRC"
git clone --quiet "$REPO_SRC" "$WORK" || { say "clone failed"; exit 1; }
cd "$WORK" || exit 1
# Tasks always start from main. No fallback: branching from anything else would
# produce a plausible-looking result built on the wrong code.
git checkout --quiet -b "visor/$RUN" origin/main || { say "no main branch in $REPO_SRC"; exit 1; }
# A second lock behind the one in push_result.sh: git itself refuses any push
# from this workspace that is not to a visor/ branch, whoever types it.
cat > "$WORK/.git/hooks/pre-push" <<'HOOK'
#!/bin/sh
while read -r local_ref local_id remote_ref remote_id; do
  case "$remote_ref" in
    refs/heads/visor/?*) ;;
    *) echo "pre-push: REFUSED: $remote_ref is not a visor/ branch" >&2; exit 1 ;;
  esac
done
HOOK
chmod +x "$WORK/.git/hooks/pre-push"

# The project's own rules for contributors, whichever name it keeps them under.
RULES=""
for f in AGENTS.md CLAUDE.md; do
  [ -f "$f" ] && { RULES="$WORK/$f"; break; }
done
[ -n "$RULES" ] || say "WARNING: no AGENTS.md or CLAUDE.md -- the agent gets no project rules"

GODOT_VER="$(godot-headless "$WORK" --version 2>/dev/null | tail -1)"
say "godot $GODOT_VER"
say "importing project"
godot-import "$WORK" > "$OUT/import.log" 2>&1
say "baseline tests"
gut-test "$WORK" > "$OUT/tests-before.log" 2>&1; BEFORE=$?
say "baseline exit=$BEFORE"

# Standing instructions go in the system prompt, because a harness keeps that
# whole when it summarises a long conversation. The first message does not
# survive a summary, and an agent that has lost the test command invents one.
if [ "$PLAN_ONLY" = 1 ]; then ROUND="plan"; ACCESS="ro"; else ROUND="build"; ACCESS="rw"; fi
{
  cat "$HERE/system-prompt.md"
  echo
  cat "$HERE/system-prompt-$ROUND.md"
  echo
  echo "# This project"
  echo "The repository is at $WORK (Godot $GODOT_VER)."
  if [ -n "$RULES" ]; then
    echo "Its own rules follow, from $(basename "$RULES"). Obey them."
    echo
    cat "$RULES"
  fi
} > "$OUT/system-prompt.txt"
{
  cat "$TASK"
  if [ -n "$NOTES" ]; then
    echo; echo "EARLIER DISCUSSION WITH THE OWNER"; cat "$NOTES"
  fi
} > "$OUT/prompt.txt"

# The two doors in the wall. Both are closed again as soon as the agent is done.
mkdir -m 700 "$WALL" || { say "could not create $WALL"; exit 1; }
python3 "$HERE/doors/model-door.py" "$WALL/model.sock" "$MODEL_SERVER" "$MODEL" "$CALLS" \
  2> "$OUT/model-door.log" &
MODEL_DOOR=$!
# -t is how long socat keeps a connection open for the answer once the request
# has arrived. Its default is half a second, and a test run takes longer.
socat -t 3600 UNIX-LISTEN:"$WALL/godot.sock",fork EXEC:"$HERE/doors/godot-door $WORK $WALL/godot.lock" \
  2> "$OUT/godot-door.log" &
GODOT_DOOR=$!
WATCHERS=""; DOORS_OPEN=1
close_doors() {
  [ "$DOORS_OPEN" = 1 ] || return 0
  DOORS_OPEN=0
  kill $MODEL_DOOR $GODOT_DOOR $WATCHERS 2>/dev/null
  # A Godot the agent started through the test door must not outlive the run.
  if flatpak ps --columns=application 2>/dev/null | grep -q org.godotengine.Godot; then
    say "WARNING: Godot was still running after the agent; stopping it"
    flatpak kill org.godotengine.Godot
  fi
  rm -rf "$WALL"
}
trap close_doors EXIT
for _ in $(seq 1 50); do
  [ -S "$WALL/model.sock" ] && [ -S "$WALL/godot.sock" ] && break
  sleep 0.1
done
[ -S "$WALL/model.sock" ] && [ -S "$WALL/godot.sock" ] \
  || { say "the doors did not open -- see $OUT/model-door.log and $OUT/godot-door.log"; exit 1; }

# Nothing the agent must not see may be visible from inside, and both doors must
# pass what they should and refuse the rest. Checked before every run.
WALLED=("$HERE/wall.sh" "$WORK" "$WALL" "$OUT/harness-log" "$ACCESS")
"${WALLED[@]}" check-wall "$ACCESS" "$WORK" "$HOME/.ssh" "$REPO_SRC" "$TASK" "$OUT/run.log" "$HERE/run_gate.sh" \
  < /dev/null > "$OUT/wall-check.log" 2>&1 \
  || { say "WALL CHECK FAILED -- the agent was not started"; cat "$OUT/wall-check.log"; exit 1; }
say "wall checked: $(grep -c '^ok' "$OUT/wall-check.log") checks passed"
# The check knocks on both doors with requests they must refuse. Those are not the agent's.
refusals() { cat "$OUT/model-door.log" "$OUT/godot-door.log" | grep -c REFUSED; }
REFUSED_BY_CHECK="$(refusals)"
# The model's answers to the agent, as the model door recorded them.
answered() { jq -c 'select(.status == 200)' "$CALLS"; }

say "agent start ($HARNESS, $ROUND round)"
T0=$(date +%s)
harness_command
# stdin must be readable: under nohup it is not, and a harness dies with EBADF.
"${WALLED[@]}" "${COMMAND[@]}" < /dev/null > "$OUT/$HARNESS_OUTPUT" 2> "$OUT/agent.err" &
AGENT_PID=$!

# Guard: the first request shows exactly which tools the harness offered.
# Anything outside the harness's list ends the run.
(
  while kill -0 "$AGENT_PID" 2>/dev/null; do
    if [ -n "$(answered | head -1)" ]; then
      extra="$(answered | head -1 | jq -r --argjson ok "$HARNESS_TOOLS" '.tools - $ok | join(",")')"
      if [ -n "$extra" ]; then
        echo "$extra" > "$OUT/unexpected-tools.txt"
        kill "$AGENT_PID"
      fi
      break
    fi
    sleep 10
  done
) &
WATCHERS="$!"

# Memory watch: one line a minute, so a run can be read afterwards as memory
# against context size. A machine that swaps hard for three minutes running is
# no longer doing useful work, and the run is stopped.
SWAP_LIMIT="${VISOR_SWAP_LIMIT:-50}"   # MB per second, in and out together
(
  swapped() { awk -v kb="$(( $(getconf PAGESIZE) / 1024 ))" '/^pswp(in|out) /{n += $2} END{print n * kb}' /proc/vmstat; }
  echo "time available_mb swap_used_mb swap_mb_per_s model_mb calls prompt_tokens" > "$OUT/memory.log"
  last="$(swapped)"; strikes=0
  while sleep 60 && kill -0 "$AGENT_PID" 2>/dev/null; do
    now="$(swapped)"; rate=$(( (now - last) / 1024 / 60 )); last="$now"
    available="$(awk '/^MemAvailable/{print int($2 / 1024)}' /proc/meminfo)"
    swap="$(awk '/^SwapTotal/{t = $2} /^SwapFree/{f = $2} END{print int((t - f) / 1024)}' /proc/meminfo)"
    # The model is held by a runner the model server starts, under a name of its own.
    # Both have "ollama" in their command line; the largest of them holds the model.
    model="$(ps -eo rss=,args= | awk '/ollama/ && $1 > m {m = $1} END{print int(m / 1024)}')"
    tokens="$(answered | tail -1 | jq -r '.prompt_tokens // "-"')"
    echo "$(date +%H:%M) $available $swap $rate $model $(answered | wc -l) ${tokens:--}" >> "$OUT/memory.log"
    if [ "$rate" -ge "$SWAP_LIMIT" ]; then
      strikes=$(( strikes + 1 ))
      say "memory: swapping at $rate MB/s, $available MB available ($strikes of 3)"
    else
      strikes=0
    fi
    if [ "$strikes" -ge 3 ]; then
      echo "swapping at $rate MB/s for three minutes running, $available MB available" > "$OUT/stopped-by-memory.txt"
      kill "$AGENT_PID"
      break
    fi
  done
) &
WATCHERS="$WATCHERS $!"

wait "$AGENT_PID"; AGENT=$?
T1=$(date +%s)
close_doors
say "agent exit=$AGENT after $(( (T1-T0)/60 )) min"
[ "$AGENT" = 0 ] || say "AGENT FAILED -- see $OUT/agent.err"
[ -f "$OUT/unexpected-tools.txt" ] \
  && say "STOPPED: the harness offered tools outside the allowed set: $(cat "$OUT/unexpected-tools.txt")"
if [ -f "$OUT/stopped-by-memory.txt" ]; then
  say "STOPPED: $(cat "$OUT/stopped-by-memory.txt")"
  ollama stop "$MODEL" || say "WARNING: could not unload $MODEL"
fi
REFUSED=$(( $(refusals) - REFUSED_BY_CHECK ))
[ "$REFUSED" = 0 ] || say "the doors refused $REFUSED requests from the agent -- see model-door.log and godot-door.log"
harness_final_message > "$OUT/final-message.md" 2> /dev/null
[ -s "$OUT/final-message.md" ] || say "WARNING: the agent left no closing message"

AFTER="n/a"; PUSHED="nothing to push"; PULL_REQUEST="none"
if [ "$PLAN_ONLY" = 0 ]; then
  say "tests after"
  gut-test "$WORK" > "$OUT/tests-after.log" 2>&1; AFTER=$?
  git add -A
  git diff --cached --stat > "$OUT/diffstat.txt"
  git diff --cached > "$OUT/changes.diff"
  # The state of the work travels with the commit, so it can be read wherever the
  # branch is looked at.
  if git -c user.name="visor" -c user.email="visor@localhost" commit --quiet \
       -m "visor gate: $NAME ($MODEL, $HARNESS)" \
       -m "Written by an agent and not yet reviewed.
agent exit: $AGENT   tests before: exit $BEFORE   tests after: exit $AFTER"; then
    if "$HERE/push_result.sh" "$REPO_SRC" "$WORK" > "$OUT/push.log" 2>&1; then
      PUSHED="yes, as visor/$RUN"
      OPEN_PULL_REQUEST=1
    else
      PUSHED="NO -- $(tail -1 "$OUT/push.log")"
      say "PUSH FAILED -- see $OUT/push.log"
    fi
  else
    say "nothing to commit"
  fi
fi

{
  echo "# $RUN"
  echo
  if [ "$AGENT" != 0 ]; then
    echo "**AGENT FAILED (exit $AGENT).** Anything below is what it left behind, not a finished result."
    [ -f "$OUT/unexpected-tools.txt" ] \
      && echo "Stopped by the tool guard. Unexpected tools: $(cat "$OUT/unexpected-tools.txt")"
    [ -f "$OUT/stopped-by-memory.txt" ] \
      && echo "Stopped by the memory watch: $(cat "$OUT/stopped-by-memory.txt")."
    echo '```'; tail -20 "$OUT/agent.err"; echo '```'
    echo
  fi
  echo "- task: $NAME"
  echo "- model: $MODEL"
  echo "- harness: $HARNESS $("$HARNESS" --version 2>/dev/null | tail -1)"
  echo "- godot: $GODOT_VER"
  echo "- round: $ROUND"
  echo "- agent minutes: $(( (T1-T0)/60 ))   agent exit: $AGENT"
  echo "- tests before: exit $BEFORE   tests after: exit $AFTER"
  echo "- model calls: $(answered | wc -l)   first prompt: $(answered | head -1 | jq -r '.prompt_tokens // "unknown"') tokens" \
       "  largest prompt: $(answered | jq -s '[.[].prompt_tokens // 0] | max // 0') tokens"
  echo "- memory: least available $(awk 'NR > 1 && (m == "" || $2 < m) {m = $2} END{print m + 0}' "$OUT/memory.log") MB," \
       "fastest swapping $(awk 'NR > 1 && $4 > m {m = $4} END{print m + 0}' "$OUT/memory.log") MB/s"
  echo "- wall: $(grep -c '^ok' "$OUT/wall-check.log") checks passed before the agent started;" \
       "the doors refused $REFUSED requests from the agent"
  echo "- branch: visor/$RUN   workspace: $WORK"
  echo "- pushed: $PUSHED"
  echo
  if [ "$PLAN_ONLY" = 0 ]; then
    echo "## Files changed"
    echo '```'; cat "$OUT/diffstat.txt" 2>/dev/null; echo '```'
    echo
  fi
  echo "## The agent's closing message"
  echo
  cat "$OUT/final-message.md"
} > "$OUT/report.md"

# The pull request carries the report, the agent's closing message included, and
# its title says at a glance whether the work is whole.
if [ "${OPEN_PULL_REQUEST:-0}" = 1 ]; then
  STATE="visor"
  [ "$AFTER" = 0 ] || STATE="visor: TESTS FAIL"
  [ "$AGENT" = 0 ] || STATE="visor: AGENT FAILED"
  TITLE="[$STATE] $(head -1 "$TASK" | cut -c1-70)"
  if PULL_REQUEST="$("$HERE/open_pr.sh" "$REPO_SRC" "$WORK" "$TITLE" "$OUT/report.md" 2> "$OUT/pull-request.log" | tail -1)" \
     && [ -n "$PULL_REQUEST" ]; then
    say "pull request: $PULL_REQUEST"
  else
    PULL_REQUEST="NOT OPENED -- $(tail -1 "$OUT/pull-request.log")"
    say "PULL REQUEST NOT OPENED -- see $OUT/pull-request.log"
  fi
fi
# Added after the pull request was opened, since the report is its body.
{ echo; echo "## Pull request"; echo; echo "$PULL_REQUEST"; } >> "$OUT/report.md"
say "done -> $OUT/report.md"
echo "GATE_RUN_DONE"
exit "$AGENT"
