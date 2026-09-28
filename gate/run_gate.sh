#!/usr/bin/env bash
# One gate run: fresh clone -> branch -> baseline tests -> agent -> tests -> report.
#
#   run_gate.sh <repo> <task.md> <model> [--plan-only] [--notes <file>]
#
# <repo>        a local clone of the project to work on; each run copies it afresh
# --plan-only   the agent may read but not edit; its plan and questions are the output
# --notes FILE  the owner's replies from an earlier plan round, appended to the prompt
set -u

REPO_SRC="$(realpath "${1:?repo required}")"; TASK="$(realpath "${2:?task file required}")"
MODEL="${3:?model required}"; shift 3
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
export PATH="$HERE/bin:$HOME/.npm-global/bin:$PATH"
NAME="$(basename "$TASK" .md)"
RUN="$NAME-$MODEL-$(date +%Y%m%d-%H%M)"
WORK="/srv/code/work/$RUN"
OUT="/srv/code/gate-results/$RUN"
mkdir -p "$OUT" /srv/code/work

say() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$OUT/run.log"; }

say "run $RUN  (plan_only=$PLAN_ONLY)"

# The harness reads its settings from the home directory. A copy that has drifted
# from the one in this repo would make runs incomparable, so refuse to start.
cmp -s "$HERE/qwen-settings.json" "$HOME/.qwen/settings.json" \
  || { say "harness settings differ from $HERE/qwen-settings.json -- run install.sh"; exit 1; }

git -C "$REPO_SRC" pull --quiet --ff-only origin main || say "WARNING: could not update $REPO_SRC"
git clone --quiet "$REPO_SRC" "$WORK" || { say "clone failed"; exit 1; }
cd "$WORK" || exit 1
# Tasks always start from main. No fallback: branching from anything else would
# produce a plausible-looking result built on the wrong code.
git checkout --quiet -b "visor/$RUN" origin/main || { say "no main branch in $REPO_SRC"; exit 1; }

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

{
  echo "You are working in the project at $WORK (Godot $GODOT_VER)."
  echo
  if [ -n "$RULES" ]; then
    echo "PROJECT RULES (from $(basename "$RULES"); follow them)"
    cat "$RULES"
    echo
  fi
  echo "TASK"
  cat "$TASK"
  if [ -n "$NOTES" ]; then
    echo; echo "EARLIER DISCUSSION WITH THE OWNER"; cat "$NOTES"
  fi
  echo
  echo "HOW TO WORK"
  if [ "$PLAN_ONLY" = 1 ]; then
    echo "- Do not edit any files in this round. Read the code, then reply with:"
    echo "  your plan, the options you considered, and numbered questions for the owner."
  else
    echo "- Read the relevant code before changing it."
    echo "- Run the test suite with the command: gut-test"
    echo "  It takes a few minutes. Every test must pass before you finish."
    echo "- Add or update tests for behaviour you change, where practical."
    echo "- Do not commit or push. Do not touch anything outside this directory."
    echo "- Finish with a short summary: what changed, which files, what you were unsure of."
  fi
} > "$OUT/prompt.txt"

MODE="yolo"; [ "$PLAN_ONLY" = 1 ] && MODE="plan"

# The only tools the agent may be offered, and the harness tools to switch off to
# get there. A harness update can add tools; the guard below catches that.
ALLOWED='["edit","glob","grep_search","read_file","run_shell_command","write_file"]'
EXCLUDED="agent,skill,update_goal,get_goal,tool_search,tool_call,notebook_edit,list_agents"
EXCLUDED="$EXCLUDED,web_fetch,web_search,enter_worktree,exit_worktree,record_artifact"
EXCLUDED="$EXCLUDED,report_findings,send_message,task_stop,cron_create,cron_delete,cron_list"
EXCLUDED="$EXCLUDED,loop_wakeup,read_mcp_resource,zoom_image,monitor"

say "agent start ($MODE)"
T0=$(date +%s)
# The prompt comes first: list-valued flags swallow any bare argument after them.
# stdin must be readable: under nohup it is not, and the harness dies with EBADF.
# --safe-mode stops the harness acting on files in the project (it will start
# whatever a project's .mcp.json names). With our own system prompt it also keeps
# the harness's share of the context near 6,000 tokens instead of 20,000.
qwen "$(cat "$OUT/prompt.txt")" \
  -m "$MODEL" --approval-mode "$MODE" --output-format json \
  --max-session-turns "${VISOR_MAX_TURNS:-150}" --max-wall-time "${VISOR_MAX_TIME:-6h}" \
  --safe-mode --system-prompt "$(cat "$HERE/system-prompt.md")" \
  --exclude-tools "$EXCLUDED" \
  --openai-logging --openai-logging-dir "$OUT/api-log" \
  < /dev/null > "$OUT/agent.json" 2> "$OUT/agent.err" &
QWEN=$!

# Guard: the first logged request shows exactly which tools the harness offered.
# Anything outside ALLOWED ends the run.
(
  while kill -0 "$QWEN" 2>/dev/null; do
    first="$(ls "$OUT"/api-log/* 2>/dev/null | head -1)"
    if [ -n "$first" ]; then
      extra="$(jq -r --argjson ok "$ALLOWED" \
        '[.request.tools[]?.function.name] - $ok | join(",")' "$first")"
      if [ -n "$extra" ]; then
        echo "$extra" > "$OUT/unexpected-tools.txt"
        kill "$QWEN"; sleep 5; kill -9 "$QWEN" 2>/dev/null
      fi
      break
    fi
    sleep 10
  done
) &
wait "$QWEN"; AGENT=$?
T1=$(date +%s)
say "agent exit=$AGENT after $(( (T1-T0)/60 )) min"
[ "$AGENT" = 0 ] || say "AGENT FAILED -- see $OUT/agent.err"
[ -f "$OUT/unexpected-tools.txt" ] \
  && say "STOPPED: the harness offered tools outside the allowed set: $(cat "$OUT/unexpected-tools.txt")"

AFTER="n/a"
if [ "$PLAN_ONLY" = 0 ]; then
  say "tests after"
  gut-test "$WORK" > "$OUT/tests-after.log" 2>&1; AFTER=$?
  git add -A
  git diff --cached --stat > "$OUT/diffstat.txt"
  git diff --cached > "$OUT/changes.diff"
  git -c user.name="visor" -c user.email="visor@localhost" commit --quiet -m "visor gate: $NAME ($MODEL)" \
    || say "nothing to commit"
fi

{
  echo "# $RUN"
  echo
  if [ "$AGENT" != 0 ]; then
    echo "**AGENT FAILED (exit $AGENT).** Anything below is what it left behind, not a finished result."
    [ -f "$OUT/unexpected-tools.txt" ] \
      && echo "Stopped by the tool guard. Unexpected tools: $(cat "$OUT/unexpected-tools.txt")"
    echo '```'; tail -20 "$OUT/agent.err"; echo '```'
    echo
  fi
  echo "- task: $NAME"
  echo "- model: $MODEL"
  echo "- godot: $GODOT_VER"
  echo "- mode: $MODE"
  echo "- agent minutes: $(( (T1-T0)/60 ))   agent exit: $AGENT"
  echo "- tests before: exit $BEFORE   tests after: exit $AFTER"
  echo "- model calls: $(ls "$OUT/api-log" 2>/dev/null | wc -l)   largest prompt: $(cat "$OUT"/api-log/* 2>/dev/null \
        | jq -s '[.[].response.usage.prompt_tokens // 0] | max // 0') tokens"
  echo "- branch: visor/$RUN   workspace: $WORK"
  echo
  echo "## Files changed"
  echo '```'; cat "$OUT/diffstat.txt" 2>/dev/null; echo '```'
} > "$OUT/report.md"
say "done -> $OUT/report.md"
echo "GATE_RUN_DONE"
exit "$AGENT"
