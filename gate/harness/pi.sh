# pi as the agent loop. Sourced by run_gate.sh, which has set
# MODEL, PLAN_ONLY and OUT.
#
# Settings: pi/settings.json and pi/models.json, which the wall puts where the
# harness looks. pi has no cap on turns, so VISOR_MAX_TURNS does not apply.

# The only tools the agent may be offered. run_gate.sh checks the first request
# against this list. pi has no plan mode: a plan-only round gets tools that
# cannot change anything.
if [ "$PLAN_ONLY" = 1 ]; then
  HARNESS_TOOLS='["find","grep","ls","read"]'
else
  HARNESS_TOOLS='["bash","edit","find","grep","read","write"]'
fi

# What the harness writes to stdout: a stream of events, one JSON object per line.
HARNESS_OUTPUT="agent-output.jsonl"

# Fills COMMAND with the program and arguments to start inside the wall.
harness_command() {
  # Everything pi would otherwise discover for itself is switched off: extensions,
  # skills, prompt templates, themes, the project's own instruction files (they are
  # already in the system prompt) and anything in the project's .pi folder.
  COMMAND=(timeout --kill-after=30 "${VISOR_MAX_TIME:-6h}"
    pi --mode json --provider ollama --model "$MODEL" --thinking off
    --system-prompt "$(cat "$OUT/system-prompt.txt")"
    --tools "$(jq -r 'join(",")' <<< "$HARNESS_TOOLS")"
    --no-extensions --no-skills --no-prompt-templates --no-themes
    --no-context-files --no-approve --offline
    --session-dir "$OUT/harness-log"
    -- "$(cat "$OUT/prompt.txt")")
}

# Prints the agent's closing message, from what the harness wrote to stdout.
harness_final_message() {
  jq -rs '[.[] | select(.type == "message_end" and .message.role == "assistant")] | last
          | .message.content[]? | select(.type == "text") | .text' "$OUT/$HARNESS_OUTPUT"
}
