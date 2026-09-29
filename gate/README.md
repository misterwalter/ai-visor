# Gate

Real tasks, run through an off-the-shelf coding agent on a CPU-only server, to
find out how much a local model can be trusted to do. Nothing here is a
pipeline. It is the test that says what is worth building.

The agent loop is off the shelf, and there are two to compare:
[Qwen Code](https://github.com/QwenLM/qwen-code) and
[pi](https://github.com/earendil-works/pi). These scripts are the wrapper
around them: a fresh copy of the project for every task, a sandbox around the
agent, tests before and after, and a report.

## What is here

| File | What it does |
|---|---|
| `install.sh` | Registers the models and the timer. Run it after every `git pull`. |
| `run_gate.sh` | One run: fresh clone, new branch, baseline tests, agent, tests again, report. |
| `watch_run.sh` | Prints one status line a minute until a run finishes. |
| `push_result.sh` | Pushes a run's result branch to the project's home. Refuses anything but a `visor/` branch. |
| `wall.sh` | Runs a command inside the sandbox. `run_gate.sh` starts the agent through it. |
| `doors/model-door.py` | The one way from the sandbox to the model. Passes chat requests, refuses the rest, records every call. |
| `doors/godot-door` | The one way from the sandbox to Godot. Accepts three requests. |
| `inside/` | The programs the agent finds inside the sandbox: `gut-test`, `godot-import`, and the self-check. |
| `system-prompt.md` | The agent's standing instructions, kept short on purpose. |
| `system-prompt-build.md`, `system-prompt-plan.md` | What is added for a build round or a plan-only round. |
| `harness/qwen.sh`, `harness/pi.sh` | How each agent loop is started, and the tools it may offer. |
| `qwen-settings.json` | Settings for Qwen Code. |
| `pi/settings.json`, `pi/models.json` | Settings for pi, and where it finds the model. |
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
- `install.sh` registers each model whose weights are already on disk and
  enables the Godot update timer. It never downloads anything, and it is safe
  to run again.
- Harness settings need no installing. The sandbox hands each harness the
  settings files in this repo every time it starts one, so a run cannot pick up
  a copy that has drifted.

Not done by `install.sh`, because each is an install you should approve yourself:

```bash
npm install -g @qwen-code/qwen-code
npm install -g @earendil-works/pi-coding-agent
flatpak install --user flathub org.godotengine.Godot
ollama pull <weights named on the FROM line of a Modelfile>
```

- `npm install -g` installs an agent loop for the current user. `-g` means
  "global", which here is the user's own npm folder, not the system. Add
  `@<version>` to the name to install one exact version.
- `flatpak install --user` installs Godot for the current user only. No sudo.
- `ollama pull` downloads model weights, about 50 GB each.

The sandbox needs `bwrap` (bubblewrap) and `socat`. Flatpak depends on the
first, and most systems ship the second. `run_gate.sh` names any program it
cannot find and stops.

## Running one task

```bash
nohup ./gate/run_gate.sh /path/to/project /path/to/task.md coder-abliterated qwen > ~/gate.log 2>&1 & disown
./gate/watch_run.sh ~/gate.log
```

- `nohup … &` starts the run in the background and keeps it alive if the SSH
  session drops. `disown` removes it from the shell's job list for the same reason.
- `> ~/gate.log 2>&1` sends both normal output and errors to one log file.
- The four arguments are the project's local clone, the task file, the Ollama
  model name, and the agent loop: `qwen` or `pi`.
- `watch_run.sh` only reads. Stopping it with Ctrl-C does not stop the run.

Options for `run_gate.sh`:

- `--plan-only` the agent may read but not edit, and the workspace is mounted
  read-only. Its output is a plan and questions.
- `--notes FILE` the owner's replies to an earlier plan round, added to the prompt.

Environment variables:

- `VISOR_MAX_TURNS` cap on agent turns (default 150). Qwen Code only; pi has no such cap.
- `VISOR_MAX_TIME` cap on wall-clock time (default `6h`).
- `VISOR_TEST_TIME` cap on one test run the agent asks for (default `15m`).
- `VISOR_SWAP_LIMIT` swapping, in MB per second, that stops a run when it lasts
  three minutes (default 50).

The run refuses to start if the project has no `main` branch or if the sandbox
fails its self-check.

## Running the tests yourself

```bash
./gate/bin/gut-test /path/to/project
./gate/bin/gut-test /path/to/project --only test_inventory
```

- The first runs the whole suite. The exit status is 0 when every test passed.
- `--only NAME` runs only the test files whose name contains `NAME`.

## Where results go

Each run is named `<task>-<model>-<harness>-<date>-<time>`.

- `/srv/code/work/<run>/` the workspace: a full clone on branch `visor/<run>`,
  with the agent's changes committed.
- The project's home (the source clone's `origin`): the same branch, pushed as
  soon as it is committed. Fetch it with `git fetch origin`.
- `/srv/code/gate-results/<run>/` the evidence:
  - `report.md` summary, with the error text on top if the agent failed
  - `final-message.md` the agent's closing message: its report, or its plan and questions
  - `changes.diff`, `diffstat.txt` what it changed
  - `tests-before.log`, `tests-after.log` the suite on either side of the change
  - `model-calls.jsonl` one line per call to the model, written by the model
    door: tools offered, tokens, seconds
  - `agent-output.json` or `.jsonl` what the harness printed, `agent.err` its errors
  - `harness-log/` the harness's own records, written from inside the sandbox
  - `system-prompt.txt`, `prompt.txt` exactly what the agent was told
  - `wall-check.log` the sandbox's self-check, run before the agent started
  - `model-door.log`, `godot-door.log` what passed through the doors and what was refused
  - `memory.log` one line a minute: free memory, swapping, model size, context in use

## What is pushed, and what cannot be

Every commit the runner makes is pushed at once, to a branch named
`visor/<run>` on the project's home. The commit message carries the agent's exit
status and the test results, and says the work is unreviewed.

Nothing here can push to `main`. There are two locks, and neither has an
option to open it:

- `push_result.sh` refuses any branch whose name does not start with `visor/`.
  It names the destination in full, so a branch can only land on the branch of
  the same name, and it never forces, so it cannot overwrite other work.
- Each workspace gets a `pre-push` hook, so git itself refuses a push to
  anything but a `visor/` branch, whoever types the command. The agent cannot
  remove the hook: `.git` is read-only inside the sandbox.

Both locks are on this machine. The account that pushes still holds a key that
the project's host would accept for `main`. A branch protection rule on the
host is the one lock that holds against everything, this repo's bugs included.

```bash
./gate/push_result.sh /path/to/project /srv/code/work/<run>
```

- Pushes the result of an earlier run by hand. The first argument is the
  project's local clone, which knows where the project's home is. The second
  is the run's workspace.

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

- **The model door** passes `POST /v1/chat/completions` for the run's model and
  refuses everything else. The model server can also pull, push and delete
  models, and a pull is an outbound request to an address of the caller's
  choosing. The door records every call in `model-calls.jsonl`, outside the
  wall, so the count of calls and tokens does not rest on the agent's honesty.
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
- The agent can alter `harness-log/`, which the harness writes from inside the
  wall. Nothing in the report is taken from it.
- It rests on the kernel's namespaces, as flatpak does. A kernel flaw could
  breach it.

## Why it is set up this way

A CPU-only machine reads a prompt at 30 to 50 tokens a second, so every token
the harness spends on itself is paid for in minutes, on every run.

| First request | Tokens | Wait before work starts |
|---|---|---|
| Harness defaults | 19,870 | about 11 minutes |
| This setup, a project's rules included | about 8,200 | 3 to 5 minutes |

With the defaults and a 32K context, the agent ran out of room after reading
three files, and the first run failed for that reason.

- **Own system prompt** in place of the built-in one, holding every standing
  instruction: how to work, the test commands, and the project's own rules from
  its `AGENTS.md` or `CLAUDE.md`. The harness keeps the system prompt whole when
  it summarises a long conversation. The first message does not survive a
  summary, and an agent that had lost its test command went looking for Godot
  across the system.
- **Only the task in the prompt.**
- **Six tools:** read, write, edit, search, find files, shell. Each harness's
  list is in `harness/`. A guard checks the first request of every run and
  stops the run if the harness offered anything else, which a harness update
  could cause.
- **Nothing discovered from the project.** Qwen Code runs with `--safe-mode`:
  without it, it will start whatever a project's `.mcp.json` names, which means
  downloading and running a package nobody approved. pi runs with its
  extensions, skills and project settings switched off.
- **Background features off** in `qwen-settings.json`. Automatic memory, memory
  consolidation and follow-up suggestions each make model calls of their own.
  With one model on one CPU they add minutes and evict the cached prompt.
- **Usage reporting off.** Web tools are among those excluded.
- **Tool output truncated** at 16,000 characters, so one large file cannot fill
  the context.
- **No cap on the length of one reply.** Qwen Code gives up on a reply after
  15 minutes unless `QWEN_STREAM_MAX_LIFETIME_MS=0` is set, and no entry in its
  settings file covers that. pi gives up after 5 minutes of silence unless
  `httpIdleTimeoutMs` is 0, and the model is silent while it reads a prompt.
- **Memory watch.** The model leaves about 5 GB of a 62 GB machine free. The
  runner logs memory every minute and stops a run that swaps hard for three
  minutes.

### When the harness summarises

A harness summarises the conversation when it nears the end of the context
window, and on this hardware one summary costs 25 to 40 minutes.

In pi the point is a setting: it summarises when the context passes the window
less `compaction.reserveTokens`, which is 49,152 tokens as set here. In Qwen
Code it is fixed inside the harness:

```
trigger = the smaller of  (threshold x window)  and  (window - 33,000)
```

- With a 65,536-token window the trigger is 32,536, whatever threshold is set.
- A window of 40,960 would trigger at 7,960, just above the size of the first
  request, so the agent would do little but summarise.
- The 33,000 is room the harness reserves for writing the summary. It cannot be
  set.

So the context window stays at 65,536, and for Qwen Code the settings that
remain are the ones that make a summary cheaper: two files restored afterwards in place of
five, and no clearing of old tool results, which would rewrite the conversation
and evict the cached prompt. Read the formula again after a harness upgrade.
