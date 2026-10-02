# Qwen Code as the agent loop. Sourced by run_gate.sh, which has set
# MODEL, ROUND and OUT.
#
# Settings: qwen-settings.json, which the wall puts where the harness looks.

# The only tools the agent may be offered. run_gate.sh checks the first request
# against this list; a harness update can add tools.
HARNESS_TOOLS='["edit","glob","grep_search","read_file","run_shell_command","write_file"]'

# What the harness writes to stdout: one JSON document.
HARNESS_OUTPUT="agent-output.json"

# Fills COMMAND with the program and arguments to start inside the wall.
harness_command() {
  # The harness tools to switch off to arrive at the list above.
  local excluded="agent,skill,update_goal,get_goal,tool_search,tool_call,notebook_edit,list_agents"
  excluded="$excluded,web_fetch,web_search,enter_worktree,exit_worktree,record_artifact"
  excluded="$excluded,report_findings,send_message,task_stop,cron_create,cron_delete,cron_list"
  excluded="$excluded,loop_wakeup,read_mcp_resource,zoom_image,monitor"
  local approval="yolo"; [ "$ROUND" = build ] || [ "$ROUND" = write ] || approval="plan"

  # The prompt comes first: list-valued flags swallow any bare argument after them.
  # --safe-mode stops the harness acting on files in the project (it will start
  # whatever a project's .mcp.json names). With our own system prompt it also keeps
  # the harness's share of the context small.
  COMMAND=(qwen "$(cat "$OUT/prompt.txt")"
    -m "$MODEL" --approval-mode "$approval" --output-format json
    --max-wall-time "${VISOR_MAX_TIME:-24h}"
    --safe-mode --system-prompt "$(cat "$OUT/system-prompt.txt")"
    --exclude-tools "$excluded"
    --openai-logging --openai-logging-dir "$OUT/harness-log")
}

# Prints the agent's closing message, from what the harness wrote to stdout.
harness_final_message() {
  jq -r '.. | .result? // empty' "$OUT/$HARNESS_OUTPUT"
}
