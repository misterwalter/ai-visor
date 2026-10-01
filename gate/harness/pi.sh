# pi as the agent loop. Sourced by run_gate.sh, which has set
# MODEL, ROUND and OUT.
#
# Settings: pi/settings.json and pi/models.json, which the wall puts where the
# harness looks. pi has no limit on time of its own, so it runs under `timeout`.

# The only tools the agent may be offered. run_gate.sh checks the first request
# against this list. pi has no plan mode: a round that changes nothing gets
# tools that cannot change anything, plus the shell, which the wall keeps
# read-only in those rounds, so that the agent can run the tests.
if [ "$ROUND" = build ]; then
  HARNESS_TOOLS='["bash","edit","find","grep","read","write"]'
else
  HARNESS_TOOLS='["bash","find","grep","ls","read"]'
fi

# What the harness writes to stdout: a stream of events, one JSON object per line.
HARNESS_OUTPUT="agent-output.jsonl"

# Fills COMMAND with the program and arguments to start inside the wall.
harness_command() {
  # Everything pi would otherwise discover for itself is switched off: extensions,
  # skills, prompt templates, themes, the project's own instruction files (they are
  # already in the system prompt) and anything in the project's .pi folder.
  COMMAND=(timeout --kill-after=30 "${VISOR_MAX_TIME:-24h}"
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
  # The closing message is often a one-line sign-off after the real answer, so
  # start from the last substantial one of the last three.
  jq -rs '[.[] | select(.type == "message_end" and .message.role == "assistant")
           | [.message.content[]? | select(.type == "text") | .text] | join("")
           | select(test("\\S"))] as $t
          | ($t | length) as $n
          | if $n == 0 then empty else
              ([range([$n - 3, 0] | max; $n)] | map(select(($t[.] | length) >= 400)) | last // ($n - 1)) as $i
              | $t[$i:] | join("\n\n") end' "$OUT/$HARNESS_OUTPUT"
}
