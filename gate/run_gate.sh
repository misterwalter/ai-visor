#!/usr/bin/env bash
# One gate run: fresh clone -> branch -> baseline tests -> agent, inside the wall
# -> tests -> report.
#
#   run_gate.sh <repo> <task.md> <model> <harness> [options]
#
# <repo>        a local clone of the project to work on; each run copies it afresh
# <harness>     the agent loop to use: qwen or pi (see harness/)
# --plan-only   the agent may read but not edit; its plan and questions are the output
# --analysis    the agent may read but not edit; its answer to the task is the output
# --notes FILE  the owner's replies from an earlier round, appended to the prompt
# --continue RUN  start from the branch an earlier run left, instead of from main
# --tests CMD   how to run the project's tests, for a project that is not Godot.
#               A Godot project (one with project.godot) uses gate/bin/gut-test.
set -u

REPO_SRC="$(realpath "${1:?repo required}")"; TASK="$(realpath "${2:?task file required}")"
MODEL="${3:?model required}"; HARNESS="${4:?harness required: qwen or pi}"; shift 4
ROUND="build"; NOTES=""; CONTINUE=""; TESTS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --plan-only) ROUND="plan" ;;
    --analysis) ROUND="analysis" ;;
    --notes) NOTES="$(realpath "$2")"; shift ;;
    --continue) CONTINUE="$2"; shift ;;
    --tests) TESTS="$2"; shift ;;
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

say "run $RUN  ($ROUND round)"

# shellcheck source=/dev/null
. "$HERE/harness/$HARNESS.sh"

for tool in bwrap socat python3 jq flock git "$HARNESS"; do
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
if [ -n "$CONTINUE" ]; then
  # A further round on an earlier run's work: its branch lives in that run's workspace.
  PREVIOUS="/srv/code/work/$CONTINUE"
  git fetch --quiet "$PREVIOUS" "visor/$CONTINUE" \
    || { say "no earlier run to continue at $PREVIOUS (branch visor/$CONTINUE)"; exit 1; }
  git checkout --quiet -b "visor/$RUN" FETCH_HEAD || { say "could not branch from the earlier run"; exit 1; }
  say "continuing $CONTINUE from $(git rev-parse --short HEAD)"
else
  # Tasks start from main. No fallback: branching from anything else would
  # produce a plausible-looking result built on the wrong code.
  git checkout --quiet -b "visor/$RUN" origin/main || { say "no main branch in $REPO_SRC"; exit 1; }
fi
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

# What kind of project: Godot gets the import step, the test door and its own
# instructions; anything else runs the test command it was given, inside the wall.
if [ -f "$WORK/project.godot" ]; then
  KIND="godot"
  command -v flatpak > /dev/null || { say "missing program: flatpak"; exit 1; }
  ENGINE="godot $(godot-headless "$WORK" --version 2>/dev/null | tail -1)"
  say "$ENGINE"
  say "importing project"
  godot-import "$WORK" > "$OUT/import.log" 2>&1
else
  KIND="plain"
  ENGINE="$(python3 --version 2>&1)"
  if [ -z "$TESTS" ] && [ "$ROUND" = build ]; then
    say "this is not a Godot project, so a build round needs --tests CMD"; exit 1
  fi
fi

if [ "$ROUND" = build ]; then ACCESS="rw"; else ACCESS="ro"; fi

# Standing instructions go in the system prompt, because a harness keeps that
# whole when it summarises a long conversation. The first message does not
# survive a summary, and an agent that has lost the test command invents one.
{
  cat "$HERE/system-prompt.md"
  echo
  cat "$HERE/system-prompt-$ROUND.md"
  if [ "$KIND" = godot ] && [ "$ROUND" = build ]; then
    echo; cat "$HERE/system-prompt-godot.md"
  elif [ -n "$TESTS" ]; then
    echo; echo "# Checking your work"
    echo "Run the tests with this command, from the repository's top folder: \`$TESTS\`"
    echo "Read the first failure before changing anything."
  fi
  echo
  echo "# This project"
  echo "The repository is at $WORK ($ENGINE)."
  if [ -n "$RULES" ]; then
    echo "Its own rules follow, from $(basename "$RULES"). Obey them."
    echo
    cat "$RULES"
  fi
} > "$OUT/system-prompt.txt"
{
  cat "$TASK"
  if [ -n "$CONTINUE" ]; then
    echo; echo "THIS BRANCH ALREADY HOLDS AN EARLIER ATTEMPT AT THE TASK."
    echo "Build on it. The discussion below says what it got right and what is missing."
  fi
  if [ -n "$NOTES" ]; then
    echo; echo "EARLIER DISCUSSION WITH THE OWNER"; cat "$NOTES"
  fi
} > "$OUT/prompt.txt"

# The two doors in the wall. Both are closed again as soon as the agent is done.
mkdir -m 700 "$WALL" || { say "could not create $WALL"; exit 1; }
[ "$KIND" = godot ] && echo "${ENGINE#godot }" > "$WALL/godot-version"   # read by inside/godot
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
  if [ "$KIND" = godot ] && flatpak ps --columns=application 2>/dev/null | grep -q org.godotengine.Godot; then
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

# The project's tests. Godot's run outside the wall, inside flatpak's own sandbox;
# any other project's run inside the wall, since a test is code the agent may have
# written. Only a build round measures them: the others change nothing.
run_tests() {
  if [ "$KIND" = godot ]; then
    gut-test "$WORK"
  else
    "${WALLED[@]}" bash -c "$TESTS" < /dev/null
  fi
}
BEFORE="n/a"
if [ "$ROUND" = build ]; then
  say "baseline tests"
  run_tests > "$OUT/tests-before.log" 2>&1; BEFORE=$?
  say "baseline exit=$BEFORE"
fi

# A model that has just been told to unload takes a while to give its memory
# back. A new load meanwhile puts two copies on a machine that holds one, and
# the swap fills in a minute. Wait for the old runner to be gone first.
for _ in $(seq 1 60); do
  if [ -z "$(ollama ps 2>/dev/null | awk 'NR > 1')" ] && pgrep -u ollama -x llama-server > /dev/null; then
    say "waiting for the previous model to unload"; sleep 5
  else
    break
  fi
done

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

# Guard: an agent that is stuck repeats itself. One made the same call 1,203
# times over six hours. The model door counts how often in a row the agent has
# made the very same tool call; past the limit, the run ends. The cap on calls
# is the backstop for a loop that varies, and the time limit the backstop for
# that. Neither is meant to end a run that is getting somewhere: the owner
# would rather wait a day for a good result.
MAX_REPEATS="${VISOR_MAX_REPEATS:-8}"
MAX_CALLS="${VISOR_MAX_CALLS:-1000}"
(
  while sleep 20 && kill -0 "$AGENT_PID" 2>/dev/null; do
    repeats="$(tail -1 "$CALLS" | jq -r '.repeats // 0')"
    calls="$(answered | wc -l)"
    if [ "${repeats:-0}" -ge "$MAX_REPEATS" ]; then
      echo "the agent made the same tool call $repeats times in a row" > "$OUT/stopped-as-stuck.txt"
    elif [ "$calls" -ge "$MAX_CALLS" ]; then
      echo "the agent made $calls calls to the model, the most a run is allowed" > "$OUT/stopped-as-stuck.txt"
    else
      continue
    fi
    kill "$AGENT_PID"
    break
  done
) &
WATCHERS="$WATCHERS $!"

# Rest: the model server's memory grows through a long run, one step at every
# summary, until the machine thrashes. A fresh model starts clean. So a run that
# has gone on this long is paused rather than failed: its work is committed and
# pushed, the model is unloaded, and the dispatcher carries on from the branch.
REST_AFTER="${VISOR_REST_AFTER:-4h}"   # 0 = never
case "$REST_AFTER" in
  0) REST_SECONDS=0 ;;
  *h) REST_SECONDS=$(( ${REST_AFTER%h} * 3600 )) ;;
  *m) REST_SECONDS=$(( ${REST_AFTER%m} * 60 )) ;;
  *) REST_SECONDS="$REST_AFTER" ;;
esac
if [ "$REST_SECONDS" -gt 0 ]; then
  (
    sleep "$REST_SECONDS"
    if kill -0 "$AGENT_PID" 2>/dev/null; then
      echo "it had run for $REST_AFTER" > "$OUT/paused.txt"
      kill "$AGENT_PID"
    fi
  ) &
  WATCHERS="$WATCHERS $!"
fi

# Memory watch: one line a minute, so a run can be read afterwards as memory
# against context size. A machine that swaps hard for three minutes running is
# no longer doing useful work, and the run is paused, as above.
SWAP_LIMIT="${VISOR_SWAP_LIMIT:-50}"   # MB per second, in and out together
(
  swapped() { awk -v kb="$(( $(getconf PAGESIZE) / 1024 ))" '/^pswp(in|out) /{n += $2} END{print n * kb}' /proc/vmstat; }
  # Swapping is one way a machine short of memory slows down. The other leaves no
  # trace in swap: the model's weights are dropped from memory and read back from
  # disk, which shows as major page faults.
  faulted() { awk '/^pgmajfault /{print $2}' /proc/vmstat; }
  echo "time available_mb swap_used_mb swap_mb_per_s major_faults_per_s model_mb calls prompt_tokens" > "$OUT/memory.log"
  last="$(swapped)"; last_faults="$(faulted)"; strikes=0
  while sleep 60 && kill -0 "$AGENT_PID" 2>/dev/null; do
    now="$(swapped)"; rate=$(( (now - last) / 1024 / 60 )); last="$now"
    now_faults="$(faulted)"; faults=$(( (now_faults - last_faults) / 60 )); last_faults="$now_faults"
    available="$(awk '/^MemAvailable/{print int($2 / 1024)}' /proc/meminfo)"
    swap="$(awk '/^SwapTotal/{t = $2} /^SwapFree/{f = $2} END{print int((t - f) / 1024)}' /proc/meminfo)"
    # The model is held by a runner the model server starts, under a name of its own.
    # Both have "ollama" in their command line; the largest of them holds the model.
    model="$(ps -eo rss=,args= | awk '/ollama/ && $1 > m {m = $1} END{print int(m / 1024)}')"
    tokens="$(answered | tail -1 | jq -r '.prompt_tokens // "-"')"
    echo "$(date +%H:%M) $available $swap $rate $faults $model $(answered | wc -l) ${tokens:--}" >> "$OUT/memory.log"
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
say "agent exit=$AGENT after $(( (T1-T0)/60 )) min"
PAUSED=""
[ -f "$OUT/paused.txt" ] && PAUSED="$(cat "$OUT/paused.txt")"
[ -f "$OUT/stopped-by-memory.txt" ] && PAUSED="the machine was short of memory: $(cat "$OUT/stopped-by-memory.txt")"
if [ -n "$PAUSED" ]; then
  say "PAUSED: $PAUSED"
  # The point of a pause: the next part starts with a fresh model.
  ollama stop "$MODEL" || say "WARNING: could not unload $MODEL"
elif [ "$AGENT" != 0 ]; then
  say "AGENT FAILED -- see $OUT/agent.err"
fi
[ -f "$OUT/unexpected-tools.txt" ] \
  && say "STOPPED: the harness offered tools outside the allowed set: $(cat "$OUT/unexpected-tools.txt")"
[ -f "$OUT/stopped-as-stuck.txt" ] && say "STOPPED: $(cat "$OUT/stopped-as-stuck.txt")"
REFUSED=$(( $(refusals) - REFUSED_BY_CHECK ))
[ "$REFUSED" = 0 ] || say "the doors refused $REFUSED requests from the agent -- see model-door.log and godot-door.log"
harness_final_message > "$OUT/final-message.md" 2> /dev/null
[ -s "$OUT/final-message.md" ] || say "WARNING: the agent left no closing message"

AFTER="n/a"; PUSHED="nothing to push"; PULL_REQUEST="none"
if [ "$ROUND" = build ]; then
  say "tests after"
  run_tests > "$OUT/tests-after.log" 2>&1; AFTER=$?
  close_doors
  git add -A
  git diff --cached --stat > "$OUT/diffstat.txt"
  git diff --cached > "$OUT/changes.diff"
  # The state of the work travels with the commit, so it can be read wherever the
  # branch is looked at.
  if git -c user.name="visor" -c user.email="visor@localhost" commit --quiet \
       -m "visor gate: $NAME ($MODEL, $HARNESS)" \
       -m "Written by an agent and not yet reviewed.${PAUSED:+ Paused, not finished: $PAUSED.}
agent exit: $AGENT   tests before: exit $BEFORE   tests after: exit $AFTER"; then
    if "$HERE/push_result.sh" "$REPO_SRC" "$WORK" > "$OUT/push.log" 2>&1; then
      PUSHED="yes, as visor/$RUN"
      # A paused part gets no pull request of its own: the part that finishes
      # opens one, and its branch holds every part's commits.
      [ -n "$PAUSED" ] || OPEN_PULL_REQUEST=1
    else
      PUSHED="NO -- $(tail -1 "$OUT/push.log")"
      say "PUSH FAILED -- see $OUT/push.log"
    fi
  else
    say "nothing to commit"
  fi
else
  close_doors
fi

{
  echo "# $RUN"
  echo
  if [ -n "$PAUSED" ]; then
    echo "**PAUSED: $PAUSED.** Not finished and not failed: the model was restarted, and the work carries on from this branch."
    echo
  elif [ "$AGENT" != 0 ]; then
    echo "**AGENT FAILED (exit $AGENT).** Anything below is what it left behind, not a finished result."
    [ -f "$OUT/unexpected-tools.txt" ] \
      && echo "Stopped by the tool guard. Unexpected tools: $(cat "$OUT/unexpected-tools.txt")"
    [ -f "$OUT/stopped-by-memory.txt" ] \
      && echo "Stopped by the memory watch: $(cat "$OUT/stopped-by-memory.txt")."
    [ -f "$OUT/stopped-as-stuck.txt" ] \
      && echo "Stopped as stuck: $(cat "$OUT/stopped-as-stuck.txt")."
    echo '```'; tail -20 "$OUT/agent.err"; echo '```'
    echo
  fi
  echo "- task: $NAME"
  echo "- model: $MODEL"
  echo "- harness: $HARNESS $("$HARNESS" --version 2>/dev/null | tail -1)"
  echo "- project: $KIND, $ENGINE"
  echo "- round: $ROUND"
  [ -n "$CONTINUE" ] && echo "- continues: $CONTINUE"
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
  [ -n "$PAUSED" ] && echo "- paused: yes"
  echo
  if [ "$ROUND" = build ]; then
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
# 75 is the usual code for "try again later": the dispatcher reads it as a pause.
[ -n "$PAUSED" ] && exit 75
exit "$AGENT"
