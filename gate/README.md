# Gate

Real tasks, run through an off-the-shelf coding agent on a CPU-only server, to
find out how much a local model can be trusted to do. Nothing here is a
pipeline. It is the test that says what is worth building.

The agent loop is [Qwen Code](https://github.com/QwenLM/qwen-code). These
scripts are the wrapper around it: a fresh copy of the project for every task,
tests before and after, and a report.

## What is here

| File | What it does |
|---|---|
| `install.sh` | Puts the settings, models and timer in place. Run it after every `git pull`. |
| `run_gate.sh` | One run: fresh clone, new branch, baseline tests, agent, tests again, report. |
| `watch_run.sh` | Prints one status line a minute until a run finishes. |
| `system-prompt.md` | The agent's standing instructions, kept short on purpose. |
| `qwen-settings.json` | Harness settings, installed as `~/.qwen/settings.json`. |
| `Modelfile.*` | Ollama recipes: which weights, context size, sampling settings. |
| `bin/godot-headless` | Runs the flatpak Godot with no window against a project folder. |
| `bin/godot-import` | Builds the `.godot` import cache a fresh clone lacks. |
| `bin/gut-test` | Runs the project's GUT suite headless. |
| `systemd/godot-update.*` | Nightly timer that updates the flatpak Godot. |

Task files are not kept in this repo. `gate/tasks/` is git-ignored; write tasks
wherever you keep your notes and pass the path.

## Setting up

Run these as the account the agent will use.

```bash
git pull
./gate/install.sh
```

- `git pull` fetches the latest version of these scripts.
- `install.sh` copies `qwen-settings.json` to `~/.qwen/settings.json`, registers
  each model whose weights are already on disk, and enables the Godot update
  timer. It never downloads anything, and it is safe to run again.

Not done by `install.sh`, because each is an install you should approve yourself:

```bash
npm install -g @qwen-code/qwen-code
flatpak install --user flathub org.godotengine.Godot
ollama pull <weights named on the FROM line of a Modelfile>
```

- `npm install -g` installs the agent for the current user. `-g` means "global",
  which here is the user's own npm folder, not the system.
- `flatpak install --user` installs Godot for the current user only. No sudo.
- `ollama pull` downloads model weights, about 50 GB each.

## Running one task

```bash
nohup ./gate/run_gate.sh /path/to/project /path/to/task.md coder-abliterated > ~/gate.log 2>&1 & disown
./gate/watch_run.sh ~/gate.log
```

- `nohup … &` starts the run in the background and keeps it alive if the SSH
  session drops. `disown` removes it from the shell's job list for the same reason.
- `> ~/gate.log 2>&1` sends both normal output and errors to one log file.
- The three arguments are the project's local clone, the task file, and the
  Ollama model name.
- `watch_run.sh` only reads. Stopping it with Ctrl-C does not stop the run.

Options for `run_gate.sh`:

- `--plan-only` the agent may read but not edit. Its output is a plan and questions.
- `--notes FILE` the owner's replies to an earlier plan round, added to the prompt.

Environment variables:

- `VISOR_MAX_TURNS` cap on agent turns (default 150).
- `VISOR_MAX_TIME` cap on wall-clock time (default `6h`).

The run refuses to start if the installed settings differ from the copy in this
repo, or if the project has no `main` branch.

## Where results go

Each run is named `<task>-<model>-<date>-<time>`.

- `/srv/code/work/<run>/` the workspace: a full clone on branch `visor/<run>`,
  with the agent's changes committed locally. Nothing is pushed.
- `/srv/code/gate-results/<run>/` the evidence:
  - `report.md` summary, with the error text on top if the agent failed
  - `changes.diff`, `diffstat.txt` what it changed
  - `tests-before.log`, `tests-after.log` the suite on either side of the change
  - `agent.json` the agent's own account, `agent.err` its errors
  - `api-log/` every request and reply between harness and model
  - `prompt.txt` exactly what the agent was told

## Why it is set up this way

A CPU-only machine reads a prompt at 30 to 50 tokens a second, so every token
the harness spends on itself is paid for in minutes, on every run.

| First request | Tokens | Wait before work starts |
|---|---|---|
| Harness defaults | 19,870 | about 11 minutes |
| This setup | about 6,500 | under 3 minutes |

With the defaults and a 32K context, the agent ran out of room after reading
three files, and the first run failed for that reason.

- **Own system prompt** (`system-prompt.md`) in place of the built-in one.
- **Six tools:** read, write, edit, search, find files, shell. The list is in
  `run_gate.sh`. A guard checks the first request of every run and stops the run
  if the harness offered anything else, which a harness update could cause.
- **`--safe-mode`.** Without it the harness acts on files it finds in the
  project. It will start whatever a project's `.mcp.json` names, which means
  downloading and running a package nobody approved.
- **Background features off** in `qwen-settings.json`. Automatic memory, memory
  consolidation and follow-up suggestions each make model calls of their own.
  With one model on one CPU they add minutes and evict the cached prompt.
- **Usage reporting off.** Web tools are among those excluded.
- **Tool output truncated** at 16,000 characters, so one large file cannot fill
  the context.
- **Project rules in the prompt.** Safe mode stops the harness reading rule
  files itself, so the runner includes the project's `AGENTS.md` or `CLAUDE.md`.

## What the agent can reach

It runs as an ordinary account with no sandbox, so it can read and write
whatever that account can. Give it an account of its own with no sudo and
nothing personal in its reach. Its shell tool can still use the network.
