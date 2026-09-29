# Gate

Real tasks, run through an off-the-shelf coding agent on a CPU-only server, to
find out how much a local model can be trusted to do. Nothing here is a
pipeline. It is the test that says what is worth building.

The agent loop is [Qwen Code](https://github.com/QwenLM/qwen-code). These
scripts are the wrapper around it: a fresh copy of the project for every task,
a sandbox around the agent, tests before and after, and a report.

## What is here

| File | What it does |
|---|---|
| `install.sh` | Puts the settings, models and timer in place. Run it after every `git pull`. |
| `run_gate.sh` | One run: fresh clone, new branch, baseline tests, agent, tests again, report. |
| `watch_run.sh` | Prints one status line a minute until a run finishes. |
| `wall.sh` | Runs a command inside the sandbox. `run_gate.sh` starts the agent through it. |
| `doors/model-door.py` | The one way from the sandbox to the model. Passes chat requests, refuses the rest. |
| `doors/godot-door` | The one way from the sandbox to Godot. Accepts three requests. |
| `inside/` | The programs the agent finds inside the sandbox: `gut-test`, `godot-import`, and the self-check. |
| `system-prompt.md` | The agent's standing instructions, kept short on purpose. |
| `system-prompt-build.md`, `system-prompt-plan.md` | What is added for a build round or a plan-only round. |
| `qwen-settings.json` | Harness settings, installed as `~/.qwen/settings.json`. |
| `Modelfile.*` | Ollama recipes: which weights, context size, sampling settings. |
| `bin/godot-headless` | Runs the flatpak Godot with no window against a project folder, walled in. |
| `bin/godot-import` | Builds the `.godot` import cache a fresh clone lacks. |
| `bin/gut-test` | Runs the project's GUT suite headless, or the test files matching a name. |
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

The sandbox needs `bwrap` (bubblewrap) and `socat`. Flatpak depends on the
first, and most systems ship the second. `run_gate.sh` names any program it
cannot find and stops.

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

- `--plan-only` the agent may read but not edit, and the workspace is mounted
  read-only. Its output is a plan and questions.
- `--notes FILE` the owner's replies to an earlier plan round, added to the prompt.

Environment variables:

- `VISOR_MAX_TURNS` cap on agent turns (default 150).
- `VISOR_MAX_TIME` cap on wall-clock time (default `6h`).
- `VISOR_TEST_TIME` cap on one test run the agent asks for (default `15m`).
- `VISOR_SWAP_LIMIT` swapping, in MB per second, that stops a run when it lasts
  three minutes (default 50).

The run refuses to start if the installed settings differ from the copy in this
repo, if the project has no `main` branch, or if the sandbox fails its self-check.

## Running the tests yourself

```bash
./gate/bin/gut-test /path/to/project
./gate/bin/gut-test /path/to/project --only test_inventory
```

- The first runs the whole suite. The exit status is 0 when every test passed.
- `--only NAME` runs only the test files whose name contains `NAME`.

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
  - `system-prompt.txt`, `prompt.txt` exactly what the agent was told
  - `wall-check.log` the sandbox's self-check, run before the agent started
  - `model-door.log`, `godot-door.log` what passed through the doors and what was refused
  - `memory.log` one line a minute: free memory, swapping, model size, context in use

## The sandbox

The agent runs with every command approved in advance, so whatever its account
can do, the model can do unobserved. An instruction to stay in its folder is a
request. The sandbox makes it a fact, enforced by the kernel.

**Wall 1, around the agent** (`wall.sh`, using bubblewrap):

| | Inside the wall |
|---|---|
| The workspace | read and write |
| The workspace's `.git` | read-only |
| System programs, the harness, its settings | read-only |
| The home folder, SSH keys, other runs, the source clone, your notes | absent |
| The account list | cut down to the agent's own account |
| Network | none |

`.git` is read-only because git runs hook scripts, and the runner uses git on
the workspace after the agent has finished. A hook written by the agent would
run outside the wall.

**Wall 2, around Godot** (`bin/godot-headless`). Tests are code the agent
writes, and Godot runs them. As shipped, the Godot flatpak grants itself the
whole filesystem, the network, and the right to run commands on the host.
`flatpak run --sandbox` drops all of that, and the wrapper hands back the
project folder alone.

**Two doors.** Godot's flatpak cannot start inside wall 1, and the harness
needs to reach the model, so the wall has two narrow openings:

- **The model door** passes `POST /v1/chat/completions` and refuses everything
  else. The model server can also pull, push and delete models, and a pull is
  an outbound request to an address of the caller's choosing.
- **The test door** accepts `import`, `test` and `test NAME`. It runs one Godot
  at a time, inside wall 2, and sends back the output.

**The self-check.** Before every run, `inside/check-wall` is run inside the
wall. It confirms that the paths which must be hidden are hidden, that `.git`,
`/usr` and `/etc` cannot be written, that the network is closed, and that each
door passes what it should and refuses what it should not. If any check fails,
the agent is not started.

**What the sandbox does not do:**

- It does not make the agent's code safe. That code runs on your machine when
  you run the project, so reviewing the diff remains the safeguard.
- It does not limit processor time or disk space.
- The agent can alter the request log of its own run, which the harness writes
  from inside the wall.
- It rests on the kernel's namespaces, as flatpak does. A kernel flaw could
  breach it.

## Why it is set up this way

A CPU-only machine reads a prompt at 30 to 50 tokens a second, so every token
the harness spends on itself is paid for in minutes, on every run.

| First request | Tokens | Wait before work starts |
|---|---|---|
| Harness defaults | 19,870 | about 11 minutes |
| This setup | about 6,500 | under 3 minutes |

With the defaults and a 32K context, the agent ran out of room after reading
three files, and the first run failed for that reason.

- **Own system prompt** in place of the built-in one, holding every standing
  instruction: how to work, the test commands, and the project's own rules from
  its `AGENTS.md` or `CLAUDE.md`. The harness keeps the system prompt whole when
  it summarises a long conversation. The first message does not survive a
  summary, and an agent that had lost its test command went looking for Godot
  across the system.
- **Only the task in the prompt.**
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
- **No cap on the length of one reply.** The harness gives up on a reply after
  15 minutes unless `QWEN_STREAM_MAX_LIFETIME_MS=0` is set, and no entry in its
  settings file covers that.
- **Memory watch.** A window that loads is not a window that fits: memory use
  grows as the context fills. The runner logs memory every minute and stops a
  run that swaps hard for three minutes.

### When the harness summarises

The harness summarises the conversation when it nears the end of the context
window, and on this hardware one summary costs 25 to 40 minutes. When it
happens is fixed inside the harness:

```
trigger = the smaller of  (threshold x window)  and  (window - 33,000)
```

- With a 65,536-token window the trigger is 32,536, whatever threshold is set.
- A window of 40,960 would trigger at 7,960, just above the size of the first
  request, so the agent would do little but summarise.
- The 33,000 is room the harness reserves for writing the summary. It cannot be
  set.

So the context window stays at 65,536, and the settings that remain are the
ones that make a summary cheaper: two files restored afterwards in place of
five, and no clearing of old tool results, which would rewrite the conversation
and evict the cached prompt. Read the formula again after a harness upgrade.
