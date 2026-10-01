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
| `dispatch.py` | The dispatcher: takes task notes, runs them, replies in them. Runs as a service. |
| `test_dispatch.py` | Its tests: `python3 gate/test_dispatch.py`. No model or Godot needed. |
| `checks.py` | Reads a build's diff for what a reviewer should look at; its findings go in the report. |
| `test_checks.py` | Its tests: `python3 gate/test_checks.py`. |
| `visor.conf.example` | The dispatcher's settings, to copy to `~/.config/visor/visor.conf`. |
| `run_gate.sh` | One run: fresh clone, new branch, baseline tests, agent, tests again, report. |
| `watch_run.sh` | Prints one status line a minute until a run finishes. |
| `push_result.sh` | Pushes a run's result branch to the project's home. Refuses anything but a `visor/` branch. |
| `open_pr.sh` | Opens a pull request for that branch, when the project's home is on GitHub. |
| `wall.sh` | Runs a command inside the sandbox. `run_gate.sh` starts the agent through it. |
| `doors/model-door.py` | The one way from the sandbox to the model. Passes chat requests, refuses the rest, records every call. |
| `doors/godot-door` | The one way from the sandbox to Godot. Accepts four requests. |
| `inside/` | The programs the agent finds inside the sandbox: `gut-test`, `godot-check`, `godot-import`, the self-check, and a `godot` that explains what to use in its place. |
| `system-prompt.md` | The agent's standing instructions, kept short on purpose. |
| `system-prompt-build.md`, `system-prompt-plan.md`, `system-prompt-analysis.md` | What is added for each kind of round. |
| `system-prompt-godot.md` | What a build round on a Godot project adds: the check commands, and Godot 3 habits to avoid. |
| `harness/qwen.sh`, `harness/pi.sh` | How each agent loop is started, and the tools it may offer. |
| `qwen-settings.json` | Settings for Qwen Code. |
| `pi/settings.json`, `pi/models.json` | Settings for pi, and where it finds the model. |
| `Modelfile.*` | Ollama recipes: which weights, context size, sampling settings. |
| `bin/godot-headless` | Runs the flatpak Godot with no window against a project folder, walled in. |
| `bin/godot-import` | Builds the `.godot` import cache a fresh clone lacks. |
| `bin/gut-test` | Runs the project's GUT suite headless, or the test files matching a name. |
| `bin/godot-check` | Compiles one script or shader inside the running project and reports its errors. |
| `godot/check_script.gd` | The script Godot runs to do that. |
| `systemd/godot-update.*` | Nightly timer that updates the flatpak Godot, never during a run. |
| `systemd/visor-dispatch.service` | Keeps the dispatcher running. |

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

## The dispatcher

The dispatcher turns notes in a shared notes folder into runs, and puts each
run's answer back in the note. The folder a note is in says whose turn it is:

| Folder | Meaning |
|---|---|
| `inbox/` | A new task. The dispatcher takes it. |
| `approved/` | You have replied. The dispatcher takes it, before anything in `inbox/`. |
| `working/` | A run is in progress. Leave the note alone. |
| `your-turn/` | The dispatcher has answered and is waiting for you. |
| `done/` | You have finished with it. |

The first lines of a note say what to do:

| Line | Meaning | If left out |
|---|---|---|
| `Project: <name>` | Which project, by its name in the settings. | The note comes back with an error. |
| `Analysis:` / `Plan:` / `Build:` | The round: answer a question, propose a plan, or make the change. | `Plan:` |
| `Model: official` / `abliterated` / `both` | Which model. `both` runs each in turn. | The setting `default_model`. |

A note replied to and moved to `approved/` gets a build round, or another
analysis round if it was a question. The whole discussion goes with it, and a
further build carries on from that model's earlier branch.

The dispatcher keeps a `sample-job.md` template in the notes folder, and puts
it back if it goes missing. It rewrites `STATUS.md` there when something changes.

```bash
cp gate/visor.conf.example ~/.config/visor/visor.conf    # then edit it
./gate/install.sh                                        # enables the service
python3 gate/dispatch.py status                          # queue, running, recent
python3 gate/dispatch.py once --dry-run                  # what it would do next
journalctl --user -u visor-dispatch -f                   # its log, live
```

- `cp` puts the example settings where the dispatcher looks. They name your
  projects, so they live on the server, not in this repository.
- `install.sh` enables and starts the service once the settings exist.
- `status` and `once --dry-run` only read. `journalctl -f` follows the log
  until Ctrl-C; the same log is kept in `~/.local/state/visor/dispatch.log`.

With `review = yes`, every finished build is followed by a review round: the
reviewer model reads the branch, the diff, the checks from the report and the
builder's own claims, and answers point by point against the task. The review
is added to the note under its own heading. To review an earlier build by hand:

```bash
python3 gate/dispatch.py review <build run> <project> [official|abliterated]
```

- Runs one read-only round on that build's branch and prints where the answer is.

Only one run happens at a time. The dispatcher waits while any other run is in
progress, including one started by hand. With `self_update = yes` it pulls this
repository while idle, reinstalls, and restarts itself.

## Running one task by hand

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
- `--analysis` the same, but the output is an answer: a question about the code,
  or advice on it, with file and line references and code samples.
- `--notes FILE` the owner's replies to an earlier round, added to the prompt.
- `--continue RUN` start from the branch an earlier run left, instead of from
  `main`, for a further round on the same work. `RUN` is the earlier run's name.
- `--fork RUN` carry on the conversation of `RUN`, an earlier part of the same
  round that was paused, instead of starting a new one. The agent is told only
  to carry on; the task is already in the conversation. pi only. For a build,
  give `--continue RUN` as well.
- `--tests CMD` how to run the tests of a project that is not Godot, for
  example `--tests "python3 tests.py"`. Required for a build round on such a
  project. The command runs inside the sandbox, before and after the agent.

A Godot project is one with a `project.godot` at its top. It gets the import
step, the test door and the Godot instructions. Any other project gets the
test command it was given, and no Godot text in its instructions.

Environment variables:

- `VISOR_MAX_REPEATS` how many times in a row the agent may make the very same
  tool call before the run is stopped as stuck (default 8).
- `VISOR_MAX_CALLS` cap on calls to the model in one run (default 1000).
- `VISOR_MAX_TIME` cap on wall-clock time (default `24h`). A backstop: the guards
  below end a run that is stuck long before this does.
- `VISOR_TEST_TIME` cap on one test run the agent asks for (default `15m`).
- `VISOR_SWAP_LIMIT` swapping, in MB per second, that stops a run when it lasts
  three minutes (default 50).
- `VISOR_REST_AFTER` how long a run goes before it is paused so the model can
  be restarted (default `4h`; `0` for never). A paused run commits and pushes
  its work but opens no pull request, and exits with status 75. Swapping past
  the limit above pauses a run the same way. The dispatcher carries a paused
  run on by itself, with the same conversation where the harness saved one
  (pi), and otherwise with a note on where it got to; by hand, use `--fork`
  and `--continue`. The first call of a carried-on part re-reads the whole
  conversation, which at 40K tokens takes most of an hour.

The run refuses to start if the project has no `main` branch or if the sandbox
fails its self-check.

## Running the tests yourself

```bash
./gate/bin/gut-test /path/to/project
./gate/bin/gut-test /path/to/project --only test_inventory
```

- The first runs the whole suite. The exit status is 0 when every test passed.
- `--only FILE` runs only the test files whose name contains `FILE`.
- `--test NAME` runs only the test functions whose name contains `NAME`, in any file.

```bash
./gate/bin/godot-check /path/to/project scripts/player.gd
```

- Compiles that one script and prints any errors with file and line. The exit
  status is 0 when it compiled. A `.gdshader` file is checked the same way;
  Godot compiles shaders even with no display.
- Godot has a `--check-only` option of its own, and it is not used here. It
  parses before the project's autoloads exist, so it fails a good script that
  names one, and it can exit 0 on a script that did not compile. This command
  loads the script inside the running project instead.

## Where results go

Each run is named `<task>-<model>-<harness>-<date>-<time>`. A plan or analysis
round changes nothing, so it commits, pushes and opens nothing; its result is
`final-message.md`.

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
    door: tools offered, tokens, seconds, and how often the agent has repeated
    itself
  - `agent-output.json` or `.jsonl` what the harness printed, `agent.err` its errors
  - `harness-log/` the harness's own records, written from inside the sandbox
  - `system-prompt.txt`, `prompt.txt` exactly what the agent was told
  - `wall-check.log` the sandbox's self-check, run before the agent started
  - `model-door.log`, `godot-door.log` what passed through the doors and what was refused
  - `memory.log` one line a minute: free memory, swapping, major page faults,
    model size, calls made, context in use

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

When the project's home is on GitHub, a pull request against `main` is opened
for the branch. Its body is the run's report, which ends with the agent's own
closing message, and its title begins `[visor]`, `[visor: TESTS FAIL]` or
`[visor: AGENT FAILED]`. Opening one changes nothing on `main`; merging it is
yours to do. It needs the `gh` program, signed in.

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
- **The test door** accepts `import`, `test`, `test NAME`, `testfn NAME` and `check FILE`. It
  runs one Godot at a time, inside wall 2, and sends back the output.

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
