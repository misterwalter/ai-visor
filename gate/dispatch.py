#!/usr/bin/env python3
"""Visor's dispatcher: turns task notes into gate runs, and gate runs into replies.

The owner writes one note per task in a shared notes folder. The folder a note
sits in says whose turn it is:

    inbox/      a new task               -> the dispatcher picks it up
    approved/   the owner has replied    -> the dispatcher picks it up
    working/    a run is in progress     -> nobody touches it
    your-turn/  the dispatcher has replied, and waits for the owner
    done/       the owner has finished with it

For each note it takes, the dispatcher reads the header, runs run_gate.sh once
per model asked for, appends each run's answer to the note, and moves the note
to your-turn/. One run at a time; notes in approved/ before notes in inbox/;
oldest first.

    dispatch.py serve      the service: loop forever
    dispatch.py once       one pass, then exit (add --dry-run to only say what it would do)
    dispatch.py status     print the queue, the current run and recent results
    dispatch.py review RUN PROJECT [MODEL]
                           review an earlier build run; prints where the review is

Settings are in ~/.config/visor/visor.conf (see visor.conf.example). State is
kept in ~/.local/state/visor/.
"""

import configparser
import datetime
import fcntl
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.parse

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import livelog  # noqa: E402
import media  # noqa: E402

ROUNDS = ("analysis", "plan", "build", "write")
# Jobs for a fixed program rather than an agent (media.py). They need no project.
MEDIA_ROUNDS = ("transcribe", "speak", "image")
# The header lines a note may have, other than the rounds.
HEADER_KEYS = ("project", "model", "thinking", "new project", "github", "tests",
               "voice", "speed", "language", "size", "count", "steps", "seed", "negative", "quality")
HEADER_RE = re.compile(r"^\s*(" + "|".join(k.replace(" ", r"\s+") for k in HEADER_KEYS + ROUNDS + MEDIA_ROUNDS)
                       + r")\s*:\s*(.*)$", re.IGNORECASE)
FOLDERS = ("inbox", "approved", "working", "your-turn", "done")
# Files in the tasks folder that are not tasks.
NOT_TASKS = {"README.md", "sample-job.md", "STATUS.md"}
# A note changed more recently than this may still be arriving through sync.
SETTLE_SECONDS = 120
# The run name is built from the note's name, and a socket path is built from the
# run name; run_gate.sh refuses one that is too long.
MAX_NAME = 24
# A run that pauses so the model can be restarted exits with this; the dispatcher
# carries it on. At most this many parts per model per round, so that a run
# which pauses at once, every time, cannot go on for ever.
PAUSED_EXIT = 75
# Harnesses that can carry on a saved conversation (run_gate.sh --fork).
HARNESSES_THAT_FORK = {"pi"}
MAX_PARTS = 6
# Every section the dispatcher adds begins this way, so the owner's own text is
# everything before the first one.
SECTION = "\n---\n\n## "
SECTION_RE = re.compile(r"^---\n\n## ", re.MULTILINE)
REPLY_HEADING = "## Your reply"

SAMPLE_JOB = """Project: {project}
Model: both
Plan:

This is a template. Copy it into inbox/ as a new note, change the lines above,
and replace this text with what you want done. If you delete or move this file,
visor puts a fresh copy back within a few minutes.

## The header

The first lines of a note tell visor what to do. Only `Project:` is required.

| Line | Meaning | If left out |
|---|---|---|
| `Project: <name>` | Which project. Visor knows: {projects}. | The note comes back to you with an error. |
| `Analysis:` | A question. Visor reads the project, changes nothing, and answers. | |
| `Plan:` | Visor reads the project, changes nothing, and replies with a plan and questions. | This is the default. |
| `Build:` | For a repository: visor makes the change straight away, on its own branch, and opens a pull request. | |
| `Write:` | For a folder project: visor writes what you ask and adds it beside your files as new drafts, `Chapter4b.md` after `Chapter4a.md`. It never changes or deletes a file of yours. | |
| `Model: official`, `abliterated`, `glimmer` or `both` | Which model does the work. `official` and `abliterated` are the two coder builds; `both` runs each in turn and gives you both answers. `glimmer` is the abliterated Muse-Glimmer, for writing: slower, better prose. | The project's own `model` setting, else `both`. |
| `Thinking: yes` or `no`, or `low`, `medium`, `high` | Whether the model thinks before it answers, and how hard. Its thinking appears in the live log. Slower. Only for models that can think, such as `glimmer`. | No thinking. |

The question or request can go on the same line as `Analysis:`, `Plan:` or
`Build:`, or below the header, or both.

## What happens next

1. Visor moves the note to `working/` while it runs. Leave it alone there.
   To watch it work, open `STATUS.md`: it links the live log, in `logs/`,
   where the model's writing and each command it runs appear about once a
   minute. Each note keeps one log per model, every round of it in order.
2. When it has an answer, it adds it to the end of the note, under a new
   heading, and moves the note to `your-turn/`.
3. To carry on, write under `## Your reply` and move the note to `approved/`.
   After a plan, that means "build it" (on a folder project, "write it"); after
   an answer, it means "look again, with what I said"; after drafts, "write
   again, with what I said". Visor reads everything in the note, not just where
   it is.
4. When you are finished with it, move the note to `done/`.

Builds never touch `main`. Each one is pushed to its own `visor/` branch with a
pull request, and a further build on the same note carries on from that branch.

## Making a new project

A note whose first line is `New project:` makes one, and runs no model:

| Line | Meaning |
|---|---|
| `New project: <name>` | The project's name, for `Project:` lines from then on. |
| `GitHub: no` | A folder project: visor makes `tasks/projects/<name>/`, and you put its files there. This is the default. |
| `GitHub: yes` | Visor clones your GitHub repository of that name to the server. |
| `GitHub: owner/repo` | Visor clones that repository instead. |
| `Model: <model>` | The model for the project's notes when they name none, for example `glimmer`. |
| `Tests: <command>` | How to run the tests of a repository that is not Godot, so visor can build on it. |

Visor answers in the note: made, and where; or why not.

## Transcription, speech and images

These need no `Project:` line. What they make goes in `tasks/media/` and is
embedded in the reply. Attachments must be somewhere in `tasks/`: in Obsidian,
set Settings, Files and links, Default location for new attachments, to "Same
folder as current file".

| Note says | What visor does |
|---|---|
| `Transcribe:` with a recording attached (`![[memo.m4a]]`) | Adds the transcript to the note. `Language: en` if the language is known. |
| `Speak:` with text below, or a note embedded (`![[Chapter2a.md]]`) | Reads it aloud into an MP3. `Voice:` (default `af_heart`; `bf_emma`, `am_michael`, `bm_george` and others), `Speed:` (1.0). |
| `Image: <prompt>`, more prompt below if wanted | Draws it. `Model:` is the image model: `realistic-vision` (default, fastest), `lustify`, `big-lust`, `pony`, `noobai`. `Size: 832x1216`, `Count: 2`, `Seed:`, `Negative:`, `Quality: full` for more steps, slower. |

Each runs walled in with no network, and all of them take time on this
machine: minutes for an image, about real time for a recording.
"""


# ── Notes ─────────────────────────────────────────────────────────────────────

def split_note(text):
    """The owner's original text, and everything the dispatcher and the owner added since."""
    match = SECTION_RE.search(text)
    if match is None:
        return text.rstrip() + "\n", ""
    start = match.start()
    return text[:start].rstrip() + "\n", text[start:].strip() + "\n"


# What a note's Thinking: line may say, and the level the harness is asked for.
THINKING = {"no": None, "off": None, "yes": "medium", "on": "medium", "low": "low", "medium": "medium", "high": "high"}


def parse_header(owner_text):
    """Project, model, round and thinking from the note's first lines. Missing ones are None."""
    header = dict.fromkeys(HEADER_KEYS + ("round", "round text"))
    for line in owner_text.splitlines()[:12]:
        match = HEADER_RE.match(line)
        if match is None:
            continue
        key, value = re.sub(r"\s+", " ", match.group(1).lower()), match.group(2).strip()
        if key in ROUNDS + MEDIA_ROUNDS:
            if not header["round"]:
                header["round"], header["round text"] = key, value
        elif not header[key]:
            header[key] = value
    return header


def note_body(owner_text):
    """The owner's text without its header lines."""
    lines = owner_text.splitlines()
    kept = [line for i, line in enumerate(lines) if not (i < 12 and HEADER_RE.match(line))]
    return "\n".join(kept).strip()


def slug(filename):
    """A short, plain name for a note, usable in a run name."""
    stem = os.path.splitext(filename)[0].lower()
    stem = re.sub(r"[^a-z0-9]+", "-", stem).strip("-") or "task"
    return stem[:MAX_NAME].rstrip("-")


def write_file(path, text):
    """Replace a file whole. Readable and writable by the owner's group, since the
    owner edits these notes from another account."""
    tmp = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".visor-tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o664)
    os.replace(tmp, path)


def append_to_note(path, section):
    # Command output can carry control characters, NUL among them; one is enough
    # for an editor to take the whole note for a binary file.
    section = "".join(c for c in section if c in "\n\t" or ord(c) >= 32)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    write_file(path, text.rstrip() + "\n" + SECTION + section.lstrip().removeprefix("## "))


def _link(path):
    """A link Obsidian follows from anywhere in the vault: the file's name, without .md."""
    return f"[[{os.path.splitext(os.path.basename(path))[0]}]]"


def move(path, folder_path):
    target = os.path.join(folder_path, os.path.basename(path))
    os.replace(path, target)
    return target


# ── The dispatcher ────────────────────────────────────────────────────────────

class ConfigError(Exception):
    pass


class ProjectError(Exception):
    """Why a project asked for in a note could not be made; told to the owner in the note."""


class Dispatcher:
    def __init__(self, config_path, state_dir, now=time.time, other_run_active=None, log=None):
        self.now = now
        self.state_dir = state_dir
        self.tasks_state = os.path.join(state_dir, "tasks")
        os.makedirs(self.tasks_state, exist_ok=True)
        self.log_path = os.path.join(state_dir, "dispatch.log")
        self.log = log or self._log_to_file
        self.other_run_active = other_run_active or _run_gate_is_running
        self._read_config(config_path)

    # Settings

    def _read_config(self, path):
        if not os.path.exists(path):
            raise ConfigError(f"no settings at {path}: copy gate/visor.conf.example there and fill it in")
        parser = configparser.ConfigParser()
        parser.read(path)
        if not parser.has_section("visor"):
            raise ConfigError(f"{path} has no [visor] section")
        visor = parser["visor"]
        self.tasks = visor.get("tasks") or _fail(f"{path}: [visor] tasks is required")
        self.harness = visor.get("harness", "pi")
        self.default_model = visor.get("default_model", "both")
        self.self_update = visor.getboolean("self_update", fallback=False)
        # After each build, a read-only round on the branch reviews it against the task.
        self.review = visor.getboolean("review", fallback=False)
        self.reviewer = visor.get("reviewer", "official")
        self.runner = visor.get("runner", os.path.join(HERE, "run_gate.sh"))
        self.results = visor.get("results", "/srv/code/gate-results")
        self.work = visor.get("work", "/srv/code/work")
        # Live logs: what each run is doing, readable in the notes while it goes.
        self.logs = visor.get("logs", os.path.join(self.tasks, "logs"))
        self.live_log_every = visor.get("live_log_every", "60")
        self.models = {"official": "coder-official", "abliterated": "coder-abliterated",
                       "glimmer": "glimmer-abliterated"}
        if parser.has_section("models"):
            self.models.update(parser["models"])
        # Projects made from a note ("New project:") are kept in a file of their own,
        # which visor writes; the settings file is the owner's and is never rewritten.
        self.config_path = path
        self.created_path = visor.get("created_projects", os.path.join(os.path.dirname(path), "projects.conf"))
        self.projects_folder = visor.get("projects_folder", os.path.join(self.tasks, "projects"))
        self.repos = visor.get("repos", "/srv/code")
        self.github_owner = visor.get("github_owner")
        self.github_url = visor.get("github_url", "git@github.com:{repo}.git")
        # Media jobs: their tools, and where what they make goes in the notes.
        self.media = media.Settings(parser["media"] if parser.has_section("media") else None, self.state_dir)
        self.media_folder = visor.get("media_folder", os.path.join(self.tasks, "media"))
        self.projects = {}
        self._add_projects(parser, path)
        if os.path.exists(self.created_path):
            made = configparser.ConfigParser()
            made.read(self.created_path)
            self._add_projects(made, self.created_path)
        for folder in FOLDERS:
            if not os.path.isdir(os.path.join(self.tasks, folder)):
                raise ConfigError(f"the tasks folder has no {folder}/: {self.tasks}")

    def _add_projects(self, parser, path):
        for section in parser.sections():
            if not section.startswith("project "):
                continue
            name = section[len("project "):].strip()
            if name in self.projects:
                _fail(f"{path}: project {name} is defined twice")
            source, folder = parser[section].get("source"), parser[section].get("folder")
            if bool(source) == bool(folder):
                _fail(f"{path}: [{section}] needs either source (a repository) or folder (a plain folder)")
            self.projects[name] = {"source": source or folder, "tests": parser[section].get("tests", ""),
                                   "folder": bool(folder), "model": parser[section].get("model")}

    def folder(self, name):
        return os.path.join(self.tasks, name)

    def can_think(self, model):
        """Whether the harness can ask this model to think: pi, with the model marked so in pi/models.json."""
        if self.harness != "pi":
            return False
        with open(os.path.join(HERE, "pi", "models.json"), encoding="utf-8") as f:
            entries = json.load(f)["providers"]["ollama"]["models"]
        entry = next((e for e in entries if e["id"] == self.models.get(model)), None)
        return bool(entry and (entry.get("compat") or {}).get("supportsReasoningEffort"))

    def live_log(self, note_name, who):
        """One log per note and model: every part and every round of it, in order."""
        return os.path.join(self.logs, f"{os.path.splitext(note_name)[0]} - {who}.md")

    def _log_to_file(self, message):
        line = f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
        print(line, flush=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    # Task state: what has been run for each note, so a later round can carry on.

    def _state_path(self, note_name):
        return os.path.join(self.tasks_state, slug(note_name) + ".json")

    def load_state(self, note_name):
        path = self._state_path(note_name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        return {"note": note_name, "runs": [], "last_build": {}, "running": None}

    def save_state(self, state):
        path = self._state_path(state["note"])
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, path)

    # The queue

    def queue(self):
        """Notes ready to take, in the order they will be taken."""
        ready = []
        for folder in ("approved", "inbox"):
            notes = []
            for name in os.listdir(self.folder(folder)):
                path = os.path.join(self.folder(folder), name)
                if not name.endswith(".md") or name.startswith(".") or not os.path.isfile(path):
                    continue
                notes.append((_changed(path), path))
            ready += [path for _, path in sorted(notes)]
        return ready

    def settled(self, path):
        return self.now() - _changed(path) >= SETTLE_SECONDS

    def next_note(self):
        for path in self.queue():
            if self.settled(path):
                return path
        return None

    # Deciding what to run

    def plan_for(self, path):
        """What to do with a note: (round, models, project, error). error is None when fine."""
        with open(path, encoding="utf-8") as f:
            owner_text, rest = split_note(f.read())
        header = parse_header(owner_text)
        came_from = os.path.basename(os.path.dirname(path))

        if not header["project"]:
            return None, [], None, "the note has no `Project:` line, so visor cannot tell which project it is for."
        if header["project"] not in self.projects:
            known = ", ".join(sorted(self.projects)) or "none yet"
            return None, [], None, f"visor does not know a project called `{header['project']}`. Known: {known}."

        model = (header["model"] or self.projects[header["project"]]["model"] or self.default_model).strip().lower()
        if model == "both":
            models = ["official", "abliterated"]
        elif model in self.models:
            models = [model]
        else:
            known = ", ".join(sorted(self.models))
            return None, [], None, f"`Model: {header['model'] or model}` is not one visor knows: use {known}, or both."

        settings = self.projects[header["project"]]
        if came_from == "approved":
            # A reply to an answer asks for another look; a reply to a plan or a build asks for a
            # build, and on a folder for more writing.
            round_ = "analysis" if header["round"] == "analysis" else ("write" if settings["folder"] else "build")
        else:
            round_ = header["round"] or "plan"
        if round_ == "build" and settings["folder"]:
            return None, [], None, (f"`{header['project']}` is a folder, not a repository: ask for `Write:` "
                                    "rather than `Build:`.")
        if round_ == "write" and not settings["folder"]:
            return None, [], None, (f"`{header['project']}` is a repository: ask for `Build:` there. `Write:` "
                                    "is for folder projects.")
        if header["thinking"] is not None:
            if header["thinking"].lower() not in THINKING:
                return None, [], None, (f"`Thinking: {header['thinking']}` is not one visor knows: use yes or no, "
                                        "or a level: low, medium or high.")
            if THINKING[header["thinking"].lower()]:
                cannot = [m for m in models if not self.can_think(m)]
                if cannot:
                    return None, [], None, (f"`Thinking: {header['thinking']}` asks for thinking, and "
                                            f"{', '.join(cannot)} cannot think. Use a model that can, such as "
                                            "glimmer, or leave the line out.")
        if round_ == "build" and not settings["tests"] and not _is_godot(settings["source"]):
            return None, [], None, (f"`{header['project']}` has no test command in visor's settings, and a build "
                                    "needs one. Ask for a plan or an analysis, or add `tests =` for it.")
        return round_, models, header["project"], None

    # Running

    def process(self, path, dry_run=False):
        """Take one note: run it, reply in it, hand it back. Returns a line for the log."""
        name = os.path.basename(path)
        with open(path, encoding="utf-8") as f:
            header = parse_header(split_note(f.read())[0])
        if header["new project"] is not None:
            if dry_run:
                return f"would create the project asked for in {name}"
            return self.create_project(path)
        if header["round"] in MEDIA_ROUNDS:
            if dry_run:
                return f"would run a {header['round']} job for {name}"
            return self.media_job(path)
        round_, models, project, error = self.plan_for(path)
        if dry_run:
            if error:
                return f"would hand back {name}: {error}"
            return f"would run a {round_} round on {project} with {', '.join(models)} for {name}"
        if error:
            append_to_note(path, f"## Visor could not start, {_stamp()}\n\n{error}\n\n"
                                 "See `sample-job.md` for how the first lines of a note work. "
                                 f"Fix the note and move it back to `inbox/`.\n\n{REPLY_HEADING}\n")
            move(path, self.folder("your-turn"))
            return f"handed back {name}: {error}"

        path = move(path, self.folder("working"))
        state = self.load_state(name)
        with open(path, encoding="utf-8") as f:
            owner_text, rest = split_note(f.read())
        thinking = THINKING.get((parse_header(owner_text)["thinking"] or "no").lower())
        task_file = os.path.join(self.tasks_state, slug(name) + ".md")
        write_file(task_file, owner_text)

        for model in models:
            parts = []
            resume, fork = "", ""
            while True:
                log = self.live_log(name, model)
                state["running"] = {"model": model, "round": round_, "started": _stamp(), "part": len(parts) + 1,
                                    "log": log}
                self.save_state(state)
                self.write_status()
                flags = {"analysis": ["--analysis"], "plan": ["--plan-only"], "build": [], "write": ["--write"]}[round_]
                if self.projects[project]["folder"]:
                    flags += ["--folder"]
                if thinking:
                    flags += ["--thinking", thinking]
                if fork:
                    # The task and the discussion are in the conversation it carries on.
                    flags += ["--fork", fork]
                else:
                    part_notes = self._notes_for_part(name, rest, resume)
                    if part_notes:
                        flags += ["--notes", part_notes]
                if round_ == "build" and state["last_build"].get(model):
                    flags += ["--continue", state["last_build"][model]]
                elif round_ == "write" and parts:
                    # Writing carries on its branch only across a pause: once a round ends, its drafts
                    # are in the folder, and the next round starts from the folder as it is.
                    flags += ["--continue", parts[-1]]
                if self.projects[project]["tests"]:
                    flags += ["--tests", self.projects[project]["tests"]]
                flags += ["--live-log", log]
                cmd = [self.runner, self.projects[project]["source"], task_file, self.models[model], self.harness] + flags
                self.log(f"{name}: {round_} round on {model}, part {len(parts) + 1}: {' '.join(cmd)}")
                run_name, exit_code = self._run(cmd)
                result = self._read_result(run_name)
                state["runs"].append({"run": run_name, "model": model, "round": round_, "exit": exit_code,
                                      "finished": _stamp(), "pull_request": result["pull_request"]})
                if round_ == "build" and run_name and result["pushed"]:
                    state["last_build"][model] = run_name
                state["running"] = None
                self.save_state(state)
                parts.append(run_name)
                if exit_code != PAUSED_EXIT or not run_name:
                    break
                if result["calls"] == 0:
                    # Another part would start from the same place and stop the same way.
                    result["message"] = (f"Visor stopped carrying this on: part {len(parts)} was paused before the "
                                         f"model answered once.\n\n{result['message']}")
                    break
                if len(parts) >= MAX_PARTS:
                    left = ("The last part's branch holds the work so far." if round_ == "build" else
                            "Below is what the last part had said when it was paused.")
                    result["message"] = (f"Visor stopped carrying this on after {MAX_PARTS} parts. {left}\n\n"
                                         f"{result['message']}")
                    break
                # The next part starts with a fresh model. Where the harness can, it carries on
                # the same conversation; otherwise this note is all it will know of the one
                # before, besides the files.
                if self.harness in HARNESSES_THAT_FORK and glob.glob(
                        os.path.join(self.results, run_name, "harness-log", "*.jsonl")):
                    fork = run_name
                else:
                    if self.harness in HARNESSES_THAT_FORK:
                        self.log(f"{name}: no conversation saved by {run_name}; the next part starts "
                                 "from a note on where it got to")
                    fork, resume = "", self._where_it_got_to(run_name, result)
            append_to_note(path, self._section(round_, model, run_name, exit_code, result, parts, log))
            if self.review and round_ == "build" and result["pushed"] and exit_code != PAUSED_EXIT:
                state["running"] = {"model": self.reviewer, "round": "review", "started": _stamp()}
                self.save_state(state)
                self.write_status()
                review_log = self.live_log(name, "review")
                review_run, review_exit, review = self.review_build(run_name, project, task_file, rest, review_log)
                state["runs"].append({"run": review_run, "model": self.reviewer, "round": "review",
                                      "exit": review_exit, "finished": _stamp(), "pull_request": "none"})
                state["running"] = None
                self.save_state(state)
                append_to_note(path, self._review_section(model, review_run, review_exit, review, review_log))

        with open(path, encoding="utf-8") as f:
            if not f.read().rstrip().endswith(REPLY_HEADING):
                append_to_note(path, f"{REPLY_HEADING}\n")
        move(path, self.folder("your-turn"))
        return f"{name}: {round_} round done on {', '.join(models)}; handed back"

    def create_project(self, path):
        """A note asking for a new project: make it, record it, and say so in the note."""
        name_of_note = os.path.basename(path)
        with open(path, encoding="utf-8") as f:
            header = parse_header(split_note(f.read())[0])
        try:
            entry, said = self._make_project(header)
        except ProjectError as error:
            append_to_note(path, f"## Visor could not create the project, {_stamp()}\n\n{error}\n\n"
                                 f"Fix the note and move it back to `inbox/`.\n\n{REPLY_HEADING}\n")
            move(path, self.folder("your-turn"))
            return f"handed back {name_of_note}: {error}"
        name = header["new project"].strip()
        lines = [f"[project {name}]"] + [f"{key} = {value}" for key, value in entry.items() if value]
        with open(self.created_path, "a", encoding="utf-8") as f:
            f.write("\n" + "\n".join(lines) + "\n")
        self._read_config(self.config_path)
        append_to_note(path, f"## Project created, {_stamp()}\n\n{said}\n\n"
                             f"Notes for it start with `Project: {name}`.\n\n{REPLY_HEADING}\n")
        move(path, self.folder("your-turn"))
        self.log(f"{name_of_note}: created project {name}")
        return f"{name_of_note}: created project {name}"

    def _make_project(self, header):
        """Make the folder or clone the repository. Returns (settings, what to tell the owner)."""
        name = header["new project"].strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,60}", name):
            raise ProjectError(f"`{name}` cannot be a project name: use letters, digits, spaces, dots, "
                               "dashes and underscores, starting with a letter or digit.")
        if name in self.projects:
            raise ProjectError(f"there is already a project called `{name}`.")
        model = (header["model"] or "").strip().lower() or None
        if model and model not in self.models:
            raise ProjectError(f"`Model: {header['model']}` is not one visor knows: use {', '.join(sorted(self.models))}.")
        github = (header["github"] or "no").strip()
        if github.lower() in ("no", "none", "false", ""):
            folder = os.path.join(self.projects_folder, name)
            existed = os.path.isdir(folder)
            os.makedirs(folder, exist_ok=True)
            os.chmod(folder, 0o2775)
            said = (f"A folder project. Its folder is `{os.path.relpath(folder, os.path.dirname(self.tasks))}`"
                    + (", which was already there." if existed else ", new and empty.")
                    + " Put what visor should read there, such as chapters and a story bible. "
                    "Ask for `Write:`, `Plan:` or `Analysis:` rounds.")
            return {"folder": folder, "model": model}, said
        if github.lower() in ("yes", "true"):
            if not self.github_owner:
                raise ProjectError("`GitHub: yes` needs the GitHub account to look in, and visor's settings "
                                   "name none (`github_owner`). Name the repository instead: `GitHub: owner/repo`.")
            repo = f"{self.github_owner}/{name}"
        elif re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", github):
            repo = github
        else:
            raise ProjectError(f"`GitHub: {github}` is not one visor understands: use yes, no, or owner/repo.")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ProjectError(f"a project cloned from GitHub becomes a folder on the server, so `{name}` "
                               "may not contain spaces.")
        target = os.path.join(self.repos, name)
        if os.path.exists(target):
            raise ProjectError(f"`{target}` already exists on the server; visor will not clone over it.")
        cloned = subprocess.run(["git", "clone", "--quiet", self.github_url.format(repo=repo), target],
                                capture_output=True, text=True, timeout=1800,
                                env=dict(os.environ, GIT_TERMINAL_PROMPT="0",
                                         GIT_SSH_COMMAND="ssh -o BatchMode=yes"))
        if cloned.returncode != 0:
            raise ProjectError(f"could not clone `{repo}` from GitHub: {cloned.stderr.strip()[-400:]}")
        tests = (header["tests"] or "").strip()
        kind = "a Godot project" if _is_godot(target) else "not a Godot project"
        said = f"Cloned `{repo}` to `{target}`: {kind}."
        if tests:
            said += f" Its tests run with `{tests}`."
        elif not _is_godot(target):
            said += (" It has no test command, so visor will plan and answer questions on it but not build. "
                     "To build, make the project again with a `Tests:` line, or ask for one to be added.")
        return {"source": target, "tests": tests, "model": model}, said

    # Media jobs

    def media_job(self, path):
        """Transcribe, speak or draw: run the tool on the note's inputs, put what it made
        in the media folder, and reply in the note."""
        name = os.path.basename(path)
        path = move(path, self.folder("working"))
        with open(path, encoding="utf-8") as f:
            owner_text, _ = split_note(f.read())
        header = parse_header(owner_text)
        kind = header["round"]
        state = self.load_state(name)
        state["running"] = {"model": kind, "round": kind, "started": _stamp(), "log": self.live_log(name, kind)}
        self.save_state(state)
        self.write_status()
        log = self.live_log(name, kind)
        os.makedirs(os.path.dirname(log), exist_ok=True)
        livelog.append(log, f"\n## {time.strftime('%Y-%m-%d %H:%M')} · {kind}\n\n*{time.strftime('%H:%M')} started*\n")
        job = os.path.join(self.media.work, f"{slug(name)}-{time.strftime('%Y%m%d-%H%M%S')}")
        os.makedirs(job)
        started, failed = time.time(), None
        try:
            self._unload_models()
            section = getattr(self, f"_media_{kind}")(name, header, owner_text, job)
        except media.MediaError as error:
            failed = str(error)
            what = {"transcribe": "transcribe this", "speak": "read this aloud", "image": "draw this"}[kind]
            section = (f"## Visor could not {what}, {_stamp()}\n\n{failed}\n\n"
                       f"Fix the note and move it back to `inbox/`. The job's files are kept in `{job}`.\n")
        minutes = round((time.time() - started) / 60)
        state["running"] = None
        state["runs"].append({"run": os.path.basename(job), "model": kind, "round": kind,
                              "exit": 1 if failed else 0, "finished": _stamp(), "pull_request": "none"})
        self.save_state(state)
        if not failed:
            shutil.rmtree(job, ignore_errors=True)
        livelog.append(log, f"\n*{time.strftime('%H:%M')}* " + ("**Failed**: " + failed[:500] if failed else "**Finished**")
                       + f" · {minutes} minutes\n")
        append_to_note(path, section.rstrip() + f"\n\n{REPLY_HEADING}\n")
        move(path, self.folder("your-turn"))
        return f"{name}: {kind} job {'FAILED' if failed else 'done'} after {minutes} minutes"

    def _media_transcribe(self, name, header, owner_text, job):
        audio = [p for p in self._attachments(owner_text) if p.lower().endswith(media.AUDIO)]
        if not audio:
            raise media.MediaError("there is no audio to transcribe: attach the recording to the note "
                                   "(`![[recording.m4a]]`), with the file somewhere in `tasks/`.")
        parts = [f"## Transcript, {_stamp()}", ""]
        for i, source in enumerate(audio):
            step = os.path.join(job, str(i))
            os.makedirs(step)
            took = time.time()
            result = media.transcribe(self.media, source, step, (header["language"] or "auto").lower())
            parts += [f"`{os.path.basename(source)}` · {result['seconds'] / 60:.1f} minutes of audio · "
                      f"took {round((time.time() - took) / 60)} minutes", "", result["text"] or "(no speech heard)", ""]
        return "\n".join(parts)

    def _media_speak(self, name, header, owner_text, job):
        notes = [p for p in self._attachments(owner_text) if p.lower().endswith(".md")]
        if notes:
            texts = []
            for note in notes:
                with open(note, encoding="utf-8", errors="replace") as f:
                    texts.append(f.read())
            source = ", ".join(f"`{os.path.basename(n)}`" for n in notes)
        else:
            texts, source = [header["round text"] or "", note_body(owner_text)], "the note"
        text = media.speech_text("\n\n".join(t for t in texts if t.strip()))
        voice, speed = (header["voice"] or "af_heart").strip(), (header["speed"] or "1.0").strip()
        language = (header["language"] or ("en-gb" if voice[:1] == "b" else "en-us")).strip().lower()
        took = time.time()
        result = media.speak(self.media, text, job, voice, speed, language)
        (saved,) = self._keep(name, "audio", result["files"])
        return (f"## Audio, {_stamp()}\n\n{source} read by `{voice}` at speed {speed} · "
                f"{result['seconds'] / 60:.1f} minutes · took {round((time.time() - took) / 60)} minutes\n\n"
                f"![[{saved}]]\n")

    def _media_image(self, name, header, owner_text, job):
        prompt = " ".join(t for t in (header["round text"], note_body(owner_text)) if t)
        options = {k: header[k] for k in ("model", "size", "count", "steps", "seed", "negative", "quality")}
        options["prompt"] = prompt
        took = time.time()
        result = media.generate(self.media, options, job)
        spec, saved = result["spec"], self._keep(name, "image", result["files"])
        settings = (f"`{spec['name']}` · {spec['width']}×{spec['height']} · {spec['steps']} steps"
                    + (" with DMD2" if spec["fast"] else "") + f" · seed {spec['seed']} · "
                    f"took {round((time.time() - took) / 60)} minutes")
        return f"## Images, {_stamp()}\n\n{settings}\n\n" + "\n".join(f"![[{s}]]" for s in saved) + "\n"

    def _attachments(self, owner_text):
        """Files the note embeds or links, found inside the tasks folder and nowhere else."""
        names = re.findall(r"!?\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]", owner_text)
        names += [urllib.parse.unquote(n) for n in re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", owner_text)]
        root = os.path.realpath(self.tasks)
        found = []
        for wanted in names:
            wanted = wanted.strip()
            hit = None
            for folder, dirs, files in os.walk(root):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for candidate in files:
                    full = os.path.join(folder, candidate)
                    if candidate == os.path.basename(wanted) and full.endswith(wanted):
                        hit = full
                        break
                if hit:
                    break
            if hit and os.path.realpath(hit).startswith(root + os.sep) and hit not in found:
                found.append(hit)
        return found

    def _keep(self, name, kind, files):
        """Copy what a job made into the media folder under names the note can embed."""
        os.makedirs(self.media_folder, exist_ok=True)
        stem, saved, n = os.path.splitext(name)[0], [], 1
        for source in files:
            ext = os.path.splitext(source)[1].lower()
            while os.path.exists(os.path.join(self.media_folder, f"{stem} {kind} {n}{ext}")):
                n += 1
            target = f"{stem} {kind} {n}{ext}"
            shutil.copyfile(source, os.path.join(self.media_folder, target))
            os.chmod(os.path.join(self.media_folder, target), 0o664)
            saved.append(target)
        return saved

    def _unload_models(self):
        """This machine holds one large model at a time: free the model server's first."""
        if not shutil.which("ollama"):
            return
        loaded = subprocess.run(["ollama", "ps"], capture_output=True, text=True).stdout.splitlines()[1:]
        for line in loaded:
            if line.split():
                subprocess.run(["ollama", "stop", line.split()[0]], capture_output=True)
        for _ in range(60):  # stopping returns before the memory is given back
            if subprocess.run(["pgrep", "-u", "ollama", "-x", "llama-server"], capture_output=True).returncode != 0:
                return
            time.sleep(2)

    def review_build(self, build_run, project, task_file, rest, log=None):
        """A read-only round on a build's branch, by the reviewer model, asked to check
        the build against the task, with the diff, the checks and the builder's claims."""
        build = self._read_result(build_run)
        base = re.search(r"^- base: (\S+)", build["report"], re.MULTILINE)
        checks = re.search(r"## Checks\s+(.*?)(?=\n## |\Z)", build["report"], re.DOTALL)
        checks = checks.group(1).strip() if checks else None
        if checks is None and base:
            # A build from before the checks existed: run them now on its workspace.
            checks = subprocess.run([sys.executable, os.path.join(HERE, "checks.py"),
                                     os.path.join(self.work, build_run), base.group(1)],
                                    capture_output=True, text=True).stdout.strip() or None
        brief = ["REVIEW THIS BUILD",
                 "",
                 "This branch holds another agent's attempt at the task above. You are its reviewer. You cannot "
                 "change anything, and you are not asked to fix it: say plainly what is right and what is not.",
                 "",
                 (f"Everything the build changed: `git diff {base.group(1)} HEAD`." if base else
                  "Its commit is the latest on this branch: `git show HEAD`."),
                 "Read the changed code itself. Run the tests yourself, with the commands in your instructions, and "
                 "say what they printed. Do not trust the builder's report below; check each claim in the code.",
                 "For each new piece, find where the program uses it, and whether anyone running the program would "
                 "ever see or reach it. Code that nothing calls, or whose effect never shows, is not done.",
                 "",
                 "Automatic checks on the diff found (pointers, not verdicts):",
                 "",
                 checks or "- (none recorded)",
                 "",
                 "The builder's own closing report:",
                 "",
                 build["message"] or "(it left none)",
                 "",
                 "Answer with:",
                 "1. A table with one row for every point the owner asked for: done, partly, missing, or changed "
                 "from what was asked; and the evidence, as file and line.",
                 "2. Anything changed that the owner did not ask for, especially values the owner gave.",
                 "3. Whether the tests exercise the new behaviour, or would pass without it.",
                 "4. Where the builder's report says something the code does not bear out.",
                 "5. One line: merge, merge after the fixes you list, or do not merge."]
        text = ((rest.strip() + "\n\n") if rest.strip() else "") + "\n".join(brief) + "\n"
        notes = os.path.join(self.tasks_state, "review-" + slug(build_run) + "-notes.md")
        write_file(notes, text)
        cmd = [self.runner, self.projects[project]["source"], task_file, self.models[self.reviewer], self.harness,
               "--analysis", "--continue", build_run, "--notes", notes]
        if log:
            cmd += ["--live-log", log]
        if self.projects[project]["tests"]:
            cmd += ["--tests", self.projects[project]["tests"]]
        self.log(f"review of {build_run} by {self.reviewer}: {' '.join(cmd)}")
        run_name, exit_code = self._run(cmd)
        return run_name, exit_code, self._read_result(run_name)

    def _review_section(self, built_by, run_name, exit_code, result, log=None):
        lines = [f"## Review of the {built_by} build, {_stamp()}, by the {self.reviewer} model", "",
                 "An agent's review. Checked against a person's on five builds, it found calls to code that does "
                 "not exist and failing tests, but missed features that nobody would ever see. A second opinion.", ""]
        if not run_name:
            return "\n".join(lines + [f"The review did not start (exit {exit_code}).", ""])
        lines += [f"`{run_name}`" + (f" · {result['summary']}" if result["summary"] else "")
                  + (f" · live log: {_link(log)}" if log else ""), ""]
        lines += [result["message"] or "(The reviewer left no answer.)", ""]
        return "\n".join(lines)

    def _notes_for_part(self, name, rest, resume):
        """The discussion so far, and for a part after a pause, where the last part got to."""
        text = rest.strip()
        if resume:
            text = (text + "\n\n" if text else "") + resume
        if not text:
            return None
        path = os.path.join(self.tasks_state, slug(name) + "-notes.md")
        write_file(path, text + "\n")
        return path

    def _where_it_got_to(self, run_name, result):
        """What a paused part left for the next one: its own last summary of its work,
        and its last few messages. Read from what the harness printed."""
        summary, messages = "", []
        try:
            with open(os.path.join(self.results, run_name, "agent-output.jsonl"), encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("type") == "compaction_end" and (event.get("result") or {}).get("summary"):
                        summary = event["result"]["summary"]
                    if event.get("type") == "message_end" and (event.get("message") or {}).get("role") == "assistant":
                        text = "".join(part.get("text", "") for part in event["message"].get("content") or []
                                       if isinstance(part, dict) and part.get("type") == "text").strip()
                        if text:
                            messages.append(text)
        except FileNotFoundError:
            pass
        paused = re.search(r"\*\*PAUSED: (.*?)\.\*\*", result["report"])
        lines = ["WHERE THE PREVIOUS PART GOT TO",
                 f"The previous part of this round was paused ({paused.group(1) if paused else 'for a rest'}) so that "
                 "the model could be restarted. Nothing went wrong. Carry on from where it stopped; do not start again."]
        if summary:
            lines += ["", "Its own last summary of the work:", "", summary.strip()]
        if messages:
            lines += ["", "Its last messages, oldest first:", ""] + [f"- {m[:600]}" for m in messages[-4:]]
        return "\n".join(lines)

    def _run(self, cmd):
        """Run one gate run. Returns its name (None if it never started) and its exit status."""
        log_path = os.path.join(self.state_dir, "run.log")
        with open(log_path, "w", encoding="utf-8") as out:
            exit_code = subprocess.call(cmd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                        env=dict(os.environ, VISOR_LIVE_LOG_EVERY=self.live_log_every))
        with open(log_path, encoding="utf-8", errors="replace") as f:
            log = f.read()
        match = re.search(r"\] run (\S+)", log)
        if match is None:
            self.log(f"the run never started (exit {exit_code}):\n{log[-2000:]}")
        return (match.group(1) if match else None), exit_code

    def _read_result(self, run_name):
        result = {"report": "", "message": "", "pull_request": "none", "pushed": False, "summary": ""}
        if not run_name:
            return result
        out = os.path.join(self.results, run_name)
        for key, filename in (("report", "report.md"), ("message", "final-message.md")):
            try:
                with open(os.path.join(out, filename), encoding="utf-8", errors="replace") as f:
                    result[key] = f.read().strip()
            except FileNotFoundError:
                pass
        report = result["report"]
        pr = re.search(r"## Pull request\s+(\S.*)", report)
        result["pull_request"] = pr.group(1).strip() if pr else "none"
        result["pushed"] = bool(re.search(r"^- pushed: yes", report, re.MULTILINE))
        result["paused"] = bool(re.search(r"^- paused: yes", report, re.MULTILINE))
        calls = re.search(r"^- model calls: (\d+)", report, re.MULTILINE)
        result["calls"] = int(calls.group(1)) if calls else None
        facts = []
        for label, pattern in (("minutes", r"agent minutes: (\d+)"), ("agent exit", r"agent exit: (\d+)"),
                               ("tests after", r"tests after: exit (\S+)")):
            m = re.search(pattern, report)
            if m and not (label == "tests after" and m.group(1) == "n/a"):
                facts.append(f"{label} {m.group(1)}")
        result["summary"] = " · ".join(facts)
        return result

    def _section(self, round_, model, run_name, exit_code, result, parts=None, log=None):
        title = {"analysis": "Answer", "plan": "Plan", "build": "Build", "write": "Drafts"}[round_]
        lines = [f"## {title}, {_stamp()}, {model} model", ""]
        if not run_name:
            lines += [f"The run did not start (exit {exit_code}). The dispatcher's log on the server says why.", ""]
            return "\n".join(lines)
        facts = [f"`{run_name}`"] + ([result["summary"]] if result["summary"] else [])
        if parts and len(parts) > 1:
            facts.append(f"in {len(parts)} parts, restarting the model between them")
        if result["pull_request"] not in ("none", ""):
            facts.append(f"pull request: {result['pull_request']}")
        if log:
            facts.append(f"live log: {_link(log)}")
        lines += [" · ".join(facts), ""]
        failed = re.search(r"^\*\*(AGENT FAILED|PAUSED).*$", result["report"], re.MULTILINE)
        if failed:
            lines += [f"> {failed.group(0)}", ""]
            stopped = re.findall(r"^Stopped .*$", result["report"], re.MULTILINE)
            lines += [f"> {s}" for s in stopped] + ([""] if stopped else [])
        drafts = re.search(r"^## Drafts\s+(.*?)(?=\n## |\Z)", result["report"], re.MULTILINE | re.DOTALL)
        if drafts:
            lines += ["Drafts:", "", drafts.group(1).strip(), ""]
        lines += [result["message"] or "(The agent left no closing message.)", ""]
        return "\n".join(lines)

    # Upkeep

    def recover(self):
        """A run the dispatcher was in when it last stopped: tell the owner, hand the note back."""
        for filename in os.listdir(self.tasks_state):
            if not filename.endswith(".json"):
                continue
            with open(os.path.join(self.tasks_state, filename), encoding="utf-8") as f:
                state = json.load(f)
            if not state.get("running"):
                continue
            path = os.path.join(self.folder("working"), state["note"])
            running = state["running"]
            state["running"] = None
            self.save_state(state)
            if os.path.exists(path):
                append_to_note(path, f"## Interrupted, {_stamp()}\n\nVisor stopped during the {running['round']} "
                                     f"round on the {running['model']} model, begun {running['started']}. "
                                     "Move this note back to where it was to try again.\n\n"
                                     f"{REPLY_HEADING}\n")
                move(path, self.folder("your-turn"))
            self.log(f"recovered {state['note']} after an interrupted {running['round']} round")

    def sample_job(self):
        """The template, naming the projects in this server's settings. Project
        names live only there: this repository is public."""
        names = sorted(self.projects)
        return SAMPLE_JOB.format(project=names[0] if names else "my-project",
                                 projects=", ".join(f"`{n}`" for n in names) or "none yet")

    def ensure_sample(self):
        path = os.path.join(self.tasks, "sample-job.md")
        if not os.path.exists(path):
            write_file(path, self.sample_job())
            self.log("wrote sample-job.md")

    def write_status(self):
        path = os.path.join(self.tasks, "STATUS.md")
        running = []
        recent = []
        for filename in sorted(os.listdir(self.tasks_state)):
            if filename.endswith(".json"):
                with open(os.path.join(self.tasks_state, filename), encoding="utf-8") as f:
                    state = json.load(f)
                if state.get("running"):
                    r = state["running"]
                    doing = (f"{r['round']} job" if r["round"] in MEDIA_ROUNDS
                             else f"{r['round']} round on the {r['model']} model")
                    running.append(f"- `{state['note']}`: {doing}, since {r['started']}"
                                   + (f" · live log: {_link(r['log'])}" if r.get("log") else ""))
                recent += [(run["finished"], state["note"], run) for run in state.get("runs", [])]
        recent.sort(key=lambda item: (item[0], item[1]), reverse=True)
        queued = [os.path.basename(p) + f" ({os.path.basename(os.path.dirname(p))})" for p in self.queue()]
        if not running and self.other_run_active():
            running = ["A run started outside visor, by hand. Visor waits for it to finish."]
        body = ["# Visor", "",
                "## Running", ""] + (running or ["Nothing."]) + ["",
                "## Waiting", ""] + ([f"- {q}" for q in queued] or ["Nothing."]) + ["",
                "## Recent", ""] + ([f"- {when}: `{note}`, {run['round']} on {run['model']}, exit {run['exit']}"
                                     for when, note, run in recent[:8]] or ["Nothing yet."]) + [""]
        text = "\n".join(body)
        old = open(path, encoding="utf-8").read() if os.path.exists(path) else None
        # Rewritten only when something changed, so sync is not kept busy.
        if old != text:
            write_file(path, text)

    def maybe_update(self):
        """Pull this repo while idle. When the code changed, reinstall and restart, so
        that the dispatcher and the runner it starts are always the same version."""
        if self.other_run_active():
            return  # never change the runner's files under a run, even one started by hand
        def git(*args):
            return subprocess.run(["git", "-C", REPO] + list(args), capture_output=True, text=True)
        if git("fetch", "--quiet", "origin").returncode != 0:
            self.log("self-update: could not fetch")
            return
        if git("rev-parse", "HEAD").stdout == git("rev-parse", "@{u}").stdout:
            return
        pulled = git("pull", "--quiet", "--ff-only")
        if pulled.returncode != 0:
            self.log(f"self-update: pull FAILED, staying on the old code: {pulled.stderr.strip()}")
            return
        self.log(f"self-update: now on {git('log', '--oneline', '-1').stdout.strip()}")
        subprocess.run([os.path.join(HERE, "install.sh")], stdout=subprocess.DEVNULL)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # The loop

    def once(self, dry_run=False):
        self.ensure_sample()
        path = self.next_note()
        if path is None:
            self.write_status()
            return None
        if self.other_run_active():
            return "another gate run is in progress; waiting"
        if dry_run:
            return self.process(path, dry_run=True)
        lock = open(os.path.join(self.state_dir, "run.lock"), "w")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            try:
                message = self.process(path)
            except Exception:
                message = f"FAILED on {os.path.basename(path)}:\n{traceback.format_exc()}"
                state = self.load_state(os.path.basename(path))
                if state.get("running"):
                    state["running"] = None
                    self.save_state(state)
                for folder in ("working", "approved", "inbox"):
                    stuck = os.path.join(self.folder(folder), os.path.basename(path))
                    if os.path.exists(stuck):
                        append_to_note(stuck, f"## Visor failed, {_stamp()}\n\nSomething went wrong in visor "
                                              "itself, not in the task. The error is in the dispatcher's log on "
                                              f"the server.\n\n{REPLY_HEADING}\n")
                        move(stuck, self.folder("your-turn"))
                        break
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        self.write_status()
        return message

    def serve(self):
        self.log("started")
        self.recover()
        last_update = 0.0
        while True:
            message = self.once()
            if message:
                self.log(message)
            if self.self_update and not message and self.now() - last_update > 600:
                last_update = self.now()
                self.maybe_update()
            time.sleep(60)


def _fail(message):
    raise ConfigError(message)


def _stamp():
    return f"{datetime.datetime.now():%Y-%m-%d %H:%M}"


def _changed(path):
    """When a note last changed or arrived. Moving a file keeps its modification
    time but updates its change time, so a note just moved in counts as new."""
    st = os.stat(path)
    return max(st.st_mtime, st.st_ctime)


def _is_godot(source):
    return os.path.exists(os.path.join(source, "project.godot"))


def _run_gate_is_running():
    return subprocess.run(["pgrep", "-u", str(os.getuid()), "-f", "run_gate.sh"],
                          stdout=subprocess.DEVNULL).returncode == 0


def main(argv):
    config = os.path.expanduser(os.environ.get("VISOR_CONFIG", "~/.config/visor/visor.conf"))
    state = os.path.expanduser(os.environ.get("VISOR_STATE", "~/.local/state/visor"))
    command = argv[1] if len(argv) > 1 else "status"
    try:
        dispatcher = Dispatcher(config, state)
    except ConfigError as error:
        print(f"visor: {error}", file=sys.stderr)
        return 1
    if command == "serve":
        dispatcher.serve()
    elif command == "once":
        print(dispatcher.once(dry_run="--dry-run" in argv) or "nothing to do")
    elif command == "review":
        if len(argv) < 4 or argv[3] not in dispatcher.projects:
            print("usage: dispatch.py review BUILD_RUN PROJECT [official|abliterated]", file=sys.stderr)
            return 2
        if len(argv) > 4:
            dispatcher.reviewer = argv[4]
        prompt = os.path.join(dispatcher.results, argv[2], "prompt.txt")
        task, rest = prompt, ""
        with open(prompt, encoding="utf-8") as f:
            given = f.read()
        # The build's prompt holds the task, and after it any discussion it was given.
        marker = given.find("\nEARLIER DISCUSSION WITH THE OWNER")
        task_text, rest = (given[:marker], given[marker:]) if marker >= 0 else (given, "")
        task = os.path.join(dispatcher.tasks_state, "review-" + slug(argv[2])[:17] + ".md")
        write_file(task, task_text.split("\nTHIS BRANCH ALREADY HOLDS")[0].rstrip() + "\n")
        run_name, exit_code, _ = dispatcher.review_build(argv[2], argv[3], task, rest)
        print(f"{run_name} exit {exit_code}: {os.path.join(dispatcher.results, run_name or '', 'final-message.md')}")
    elif command == "status":
        dispatcher.write_status()
        print(open(os.path.join(dispatcher.tasks, "STATUS.md"), encoding="utf-8").read())
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
