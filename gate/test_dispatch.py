#!/usr/bin/env python3
"""Tests for dispatch.py, against a throwaway notes folder and a stand-in for
run_gate.sh that records how it was called and writes a result. No model, no
Godot, no network.

    python3 gate/test_dispatch.py
"""

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import dispatch  # noqa: E402

FAKE_RUNNER = textwrap.dedent('''\
    #!/usr/bin/env python3
    # Stands in for run_gate.sh: records its arguments, writes a result like the real one.
    import json, os, sys
    results, calls = os.environ["FAKE_RESULTS"], os.environ["FAKE_CALLS"]
    args = sys.argv[1:]
    n = sum(1 for _ in open(calls)) if os.path.exists(calls) else 0
    run = f"{os.path.basename(args[1])[:-3]}-{args[2]}-{args[3]}-run{n}"
    notes = open(args[args.index("--notes") + 1]).read() if "--notes" in args else ""
    with open(calls, "a") as f:
        f.write(json.dumps(args + ["NOTES=" + notes]) + "\\n")
    out = os.path.join(results, run)
    os.makedirs(out)
    if os.environ.get("FAKE_STOPS_EARLY"):
        # As the real one does when it stops before the agent: a log, and no report.
        open(os.path.join(out, "run.log"), "w").write(
            f"[12:00:00] run {run}  (round)\\n[12:00:01] {os.environ['FAKE_STOPS_EARLY']}\\n")
        print(f"[12:00:00] run {run}  (round)")
        sys.exit(1)
    build = "--analysis" not in args and "--plan-only" not in args and "--write" not in args
    exit_code = int(os.environ.get("FAKE_EXIT", "0"))
    paused = n < int(os.environ.get("FAKE_PAUSES", "0"))
    if paused:
        exit_code = 75
    report = [f"# {run}", ""]
    if paused:
        report += ["**PAUSED: it had run for 4h.** Not finished and not failed.", ""]
    elif exit_code:
        report += [f"**AGENT FAILED (exit {exit_code}).** Anything below is what it left behind.", "Stopped as stuck: the same call 8 times.", ""]
    report += [f"- agent minutes: 7   agent exit: {exit_code}",
               f"- model calls: {os.environ.get('FAKE_CALLS_MADE', '12')}   first prompt: 4000 tokens   largest prompt: 9000 tokens",
               f"- tests before: exit 0   tests after: exit {'0' if build else 'n/a'}",
               f"- pushed: {'yes, as visor/' + run if build else 'nothing to push'}"]
    if paused:
        report += ["- paused: yes"]
    if "--web" in args:
        report += ["- web: 2 connections through the VPN to 1 hosts: example.org"]
    report += ["- base: abc123", ""]
    if build:
        report += ["## Checks", "", "- **numbers changed:** `x.gd:3`: 80 → 140", ""]
    if "--write" in args:
        report += ["## Drafts", "", "- `Chapter4b.md` (draft of `Chapter4a.md`)", ""]
    report += ["## Pull request", "", f"https://example.invalid/pull/{n}" if build and not paused else "none"]
    with open(os.path.join(out, "agent-output.jsonl"), "w") as f:
        f.write(json.dumps({"type": "compaction_end", "result": {"summary": f"Summary from part {n}."}}) + "\\n")
        f.write(json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": f"Working on it, call {n}."}]}}) + "\\n")
    if os.environ.get("FAKE_SESSION"):
        os.makedirs(os.path.join(out, "harness-log"))
        open(os.path.join(out, "harness-log", f"conversation-{n}.jsonl"), "w").write("{}\\n")
    open(os.path.join(out, "report.md"), "w").write("\\n".join(report) + "\\n")
    open(os.path.join(out, "final-message.md"), "w").write(f"Answer from {args[2]}.\\n")
    print(f"[12:00:00] run {run}  (round)")
    sys.exit(exit_code)
    ''')


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.tasks = os.path.join(root, "tasks")
        for folder in dispatch.FOLDERS:
            os.makedirs(os.path.join(self.tasks, folder))
        self.results = os.path.join(root, "results")
        os.makedirs(self.results)
        self.calls = os.path.join(root, "calls.jsonl")
        runner = os.path.join(root, "fake_runner.py")
        with open(runner, "w") as f:
            f.write(FAKE_RUNNER)
        os.chmod(runner, 0o755)
        self.godot = os.path.join(root, "godot-project")
        os.makedirs(self.godot)
        open(os.path.join(self.godot, "project.godot"), "w").close()
        self.config = os.path.join(root, "visor.conf")
        with open(self.config, "w") as f:
            f.write(textwrap.dedent(f"""\
                [visor]
                tasks = {self.tasks}
                harness = pi
                default_model = both
                runner = {runner}
                results = {self.results}

                [project game]
                source = {self.godot}

                [project textgame]
                source = {root}/textgame
                tests = python3 tests.py

                [project notests]
                source = {root}/notests

                [project story]
                folder = {root}/story
                model = glimmer
                """))
        os.environ.update(FAKE_RESULTS=self.results, FAKE_CALLS=self.calls)
        os.environ.pop("FAKE_EXIT", None)
        os.environ.pop("FAKE_PAUSES", None)
        os.environ.pop("FAKE_SESSION", None)
        os.environ.pop("FAKE_CALLS_MADE", None)
        os.environ.pop("FAKE_STOPS_EARLY", None)
        self.state = os.path.join(root, "state")
        self.other_run = False
        self.logged = []
        self.clock = time.time() + 1000  # every note has settled unless a test says otherwise
        self.d = dispatch.Dispatcher(self.config, self.state, now=lambda: self.clock,
                                     other_run_active=lambda: self.other_run, log=self.logged.append)
        self.vpn = True  # the tunnel is up unless a test says otherwise
        self.d.vpn_up = lambda: self.vpn

    def tearDown(self):
        self.tmp.cleanup()

    def note(self, folder, name, text):
        path = os.path.join(self.tasks, folder, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def read(self, folder, name):
        with open(os.path.join(self.tasks, folder, name)) as f:
            return f.read()

    def calls_made(self):
        if not os.path.exists(self.calls):
            return []
        return [json.loads(line) for line in open(self.calls)]

    # The header

    def test_the_owners_own_format_parses(self):
        text = "Project: my-game\nAnalysis: Take a look through the regions and score them.\n"
        header = dispatch.parse_header(text)
        self.assertEqual({k: header[k] for k in ("project", "model", "round", "thinking")},
                         {"project": "my-game", "model": None, "round": "analysis", "thinking": None})

    def test_header_keys_are_case_blind_and_the_first_round_word_wins(self):
        text = "project: game\nMODEL: Official\nbuild:\nPlan: not this\n"
        header = dispatch.parse_header(text)
        self.assertEqual({k: header[k] for k in ("project", "model", "round")},
                         {"project": "game", "model": "Official", "round": "build"})

    def test_the_owners_text_ends_where_visors_first_section_begins(self):
        text = "Project: game\nDo a thing.\n\n---\n\n## Plan, today, official model\n\nA plan.\n"
        owner, rest = dispatch.split_note(text)
        self.assertEqual(owner, "Project: game\nDo a thing.\n")
        self.assertTrue(rest.startswith("---\n\n## Plan"))

    def test_a_long_note_name_is_cut_to_fit_a_run_name(self):
        self.assertEqual(dispatch.slug("1 Region Location Rating, the long version.md"), "1-region-location-rating")
        self.assertLessEqual(len(dispatch.slug("x" * 80 + ".md")), dispatch.MAX_NAME)

    # A new task

    def test_a_new_task_gets_a_plan_round_on_both_models_and_comes_back(self):
        self.note("inbox", "task.md", "Project: game\nMake the thing.\n")
        self.d.once()
        calls = self.calls_made()
        self.assertEqual([c[2] for c in calls], ["coder-official", "coder-abliterated"])
        self.assertTrue(all("--plan-only" in c for c in calls))
        self.assertFalse(any("--notes" in c for c in calls), "a fresh task has nothing to add")
        note = self.read("your-turn", "task.md")
        self.assertTrue(note.startswith("Project: game\nMake the thing.\n"), "the owner's text is kept as it was")
        self.assertIn("## Plan, ", note)
        self.assertIn("official model", note)
        self.assertIn("abliterated model", note)
        self.assertIn("Answer from coder-official.", note)
        self.assertTrue(note.rstrip().endswith(dispatch.REPLY_HEADING))
        self.assertEqual(os.listdir(os.path.join(self.tasks, "inbox")), [])

    def test_the_agent_is_given_only_the_owners_text_as_its_task(self):
        self.note("inbox", "task.md", "Project: game\nMake the thing.\n")
        self.d.once()
        with open(self.calls_made()[0][1]) as f:
            self.assertEqual(f.read(), "Project: game\nMake the thing.\n")

    def test_an_analysis_question_gets_an_analysis_round(self):
        self.note("inbox", "q.md", "Project: textgame\nModel: official\nAnalysis: why is it slow?\n")
        self.d.once()
        (call,) = self.calls_made()
        self.assertIn("--analysis", call)
        self.assertEqual(call[call.index("--tests") + 1], "python3 tests.py")
        self.assertIn("## Answer, ", self.read("your-turn", "q.md"))

    def test_a_note_still_arriving_is_left_until_it_settles(self):
        path = self.note("inbox", "task.md", "Project: game\n")
        self.clock = dispatch._changed(path) + 30
        self.assertIsNone(self.d.once())
        self.assertEqual(self.calls_made(), [])
        self.clock += dispatch.SETTLE_SECONDS
        self.d.once()
        self.assertEqual(len(self.calls_made()), 2)

    def test_approved_notes_go_before_new_ones(self):
        self.note("inbox", "new.md", "Project: game\nModel: official\n")
        self.note("approved", "waiting.md", "Project: game\nModel: official\n")
        self.d.once()
        self.assertTrue(os.path.exists(os.path.join(self.tasks, "your-turn", "waiting.md")))
        self.assertTrue(os.path.exists(os.path.join(self.tasks, "inbox", "new.md")))

    def test_nothing_starts_while_another_gate_run_is_going(self):
        self.note("inbox", "task.md", "Project: game\n")
        self.other_run = True
        self.assertIn("waiting", self.d.once())
        self.assertEqual(self.calls_made(), [])

    # Replies

    def test_an_approved_plan_becomes_a_build_carrying_the_whole_discussion(self):
        self.note("inbox", "task.md", "Project: game\nModel: official\nMake the thing.\n")
        self.d.once()
        note = self.read("your-turn", "task.md") + "\nYes, but blue.\n"
        os.remove(os.path.join(self.tasks, "your-turn", "task.md"))
        self.note("approved", "task.md", note)
        self.d.once()
        build = self.calls_made()[-1]
        self.assertNotIn("--plan-only", build)
        self.assertNotIn("--continue", build, "the first build starts from main")
        with open(build[build.index("--notes") + 1]) as f:
            notes = f.read()
        self.assertIn("Answer from coder-official.", notes)
        self.assertIn("Yes, but blue.", notes)
        self.assertNotIn("Make the thing.", notes, "the task itself is not repeated as a note")
        self.assertIn("pull request: https://example.invalid/pull/", self.read("your-turn", "task.md"))

    def test_a_second_build_carries_on_from_each_models_own_branch(self):
        self.note("inbox", "task.md", "Project: game\nBuild:\nMake the thing.\n")
        self.d.once()
        first = self.calls_made()
        note = self.read("your-turn", "task.md") + "\nAgain.\n"
        os.remove(os.path.join(self.tasks, "your-turn", "task.md"))
        self.note("approved", "task.md", note)
        self.d.once()
        second = self.calls_made()[2:]
        for before, after in zip(first, second):
            self.assertEqual(before[2], after[2])
            earlier_run = f"task-{before[2]}-pi-run{first.index(before)}"
            self.assertEqual(after[after.index("--continue") + 1], earlier_run)

    def test_a_reply_to_an_answer_asks_for_another_look_not_a_build(self):
        self.note("approved", "q.md", "Project: game\nModel: official\nAnalysis: why?\n\n---\n\n## Answer, x\n\nBecause.\n\n## Your reply\n\nBut why?\n")
        self.d.once()
        (call,) = self.calls_made()
        self.assertIn("--analysis", call)
        self.assertIn("--notes", call)

    # Media jobs: a fixed program on the note's inputs, no agent and no project

    def made(self, job, name, data=b"x"):
        path = os.path.join(job, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_a_recording_attached_to_a_note_is_transcribed_into_it(self):
        with open(os.path.join(self.tasks, "inbox", "memo.m4a"), "wb") as f:
            f.write(b"audio")
        self.note("inbox", "memo.md", "Transcribe:\nLanguage: en\n\n![[memo.m4a]]\n")
        seen = {}
        def fake(settings, audio, job, language):
            seen.update(audio=audio, language=language)
            return {"text": "Hello, this is the memo.", "seconds": 90}
        with mock.patch.object(dispatch.media, "transcribe", fake):
            self.d.once()
        self.assertEqual(self.calls_made(), [], "no agent run")
        self.assertTrue(seen["audio"].endswith(os.path.join("inbox", "memo.m4a")))
        self.assertEqual(seen["language"], "en")
        note = self.read("your-turn", "memo.md")
        self.assertIn("## Transcript, ", note)
        self.assertIn("`memo.m4a` · 1.5 minutes of audio", note)
        self.assertIn("Hello, this is the memo.", note)

    def test_only_files_inside_the_tasks_folder_are_read(self):
        outside = os.path.join(self.tmp.name, "private.m4a")
        open(outside, "wb").close()
        self.note("inbox", "memo.md", "Transcribe:\n\n![[../../private.m4a]]\n![[private.m4a]]\n")
        with mock.patch.object(dispatch.media, "transcribe", side_effect=AssertionError("must not run")):
            self.d.once()
        self.assertIn("there is no audio to transcribe", self.read("your-turn", "memo.md"))

    def test_an_embedded_chapter_is_read_aloud_and_the_audio_embedded(self):
        story = os.path.join(self.tasks, "projects", "Story")
        os.makedirs(story)
        with open(os.path.join(story, "Chapter2a.md"), "w") as f:
            f.write("# Chapter 2\n\nThe **boat** comes in.\n")
        self.note("inbox", "read.md", "Speak:\nVoice: bf_emma\n\n![[Chapter2a.md]]\n")
        seen = {}
        def fake(settings, text, job, voice, speed, language):
            seen.update(text=text, voice=voice, language=language)
            return {"files": [self.made(job, "speech.mp3")], "seconds": 30}
        with mock.patch.object(dispatch.media, "speak", fake):
            self.d.once()
        self.assertEqual(seen, {"text": "Chapter 2\n\nThe boat comes in.", "voice": "bf_emma", "language": "en-gb"})
        note = self.read("your-turn", "read.md")
        self.assertIn("![[read audio 1.mp3]]", note)
        self.assertTrue(os.path.exists(os.path.join(self.tasks, "media", "read audio 1.mp3")))

    def test_a_note_embedded_as_obsidian_writes_it_without_md_is_found_and_read(self):
        story = os.path.join(self.tasks, "projects", "Story")
        os.makedirs(story)
        with open(os.path.join(story, "Chapter 1 - The Boat.md"), "w") as f:
            f.write("The boat comes in.\n")
        self.note("inbox", "read.md", "Project: story\nSpeak:\n\nChapter 1: The Boat\n![[Chapter 1 - The Boat]]\n")
        seen = {}
        def fake(settings, text, job, voice, speed, language):
            seen.update(text=text)
            return {"files": [self.made(job, "speech.mp3")], "seconds": 30}
        with mock.patch.object(dispatch.media, "speak", fake):
            self.d.once()
        self.assertEqual(seen, {"text": "The boat comes in."})
        self.assertIn("`Chapter 1 - The Boat.md` read by", self.read("your-turn", "read.md"))

    def test_an_embed_that_cannot_be_found_is_named_and_nothing_is_read(self):
        self.note("inbox", "read.md", "Speak:\n\nChapter 1: The Boat\n![[Chapter 1 - The Bote]]\n")
        with mock.patch.object(dispatch.media, "speak", side_effect=AssertionError("must not run")):
            self.d.once()
        note = self.read("your-turn", "read.md")
        self.assertIn("the note embeds `Chapter 1 - The Bote`, and no file of that name is anywhere in `tasks/`", note)

    def test_images_are_drawn_from_the_prompt_and_embedded(self):
        self.note("inbox", "pic.md", "Image: a lighthouse at dusk\nModel: pony\nCount: 2\n\nrain, wide shot\n")
        seen = {}
        def fake(settings, options, job):
            seen.update(options)
            spec = {"name": "pony", "width": 1024, "height": 1024, "steps": 8, "fast": True, "seed": 7}
            return {"files": [self.made(job, "a.png"), self.made(job, "b.png")], "spec": spec}
        with mock.patch.object(dispatch.media, "generate", fake):
            self.d.once()
        self.assertEqual(seen["prompt"], "a lighthouse at dusk rain, wide shot")
        self.assertEqual((seen["model"], seen["count"]), ("pony", "2"))
        note = self.read("your-turn", "pic.md")
        self.assertIn("`pony` · 1024×1024 · 8 steps with DMD2 · seed 7", note)
        self.assertIn("![[pic image 1.png]]", note)
        self.assertIn("![[pic image 2.png]]", note)

    def test_a_media_job_that_fails_says_why_and_keeps_its_files(self):
        self.note("inbox", "pic.md", "Image: a fox\n")
        with mock.patch.object(dispatch.media, "generate", side_effect=dispatch.media.MediaError("ComfyUI stopped")):
            self.d.once()
        note = self.read("your-turn", "pic.md")
        self.assertIn("## Visor could not draw this", note)
        self.assertIn("ComfyUI stopped", note)
        kept = re.search(r"kept in `([^`]+)`", note).group(1)
        self.assertTrue(os.path.isdir(kept))

    # Making a project from a note

    def make_github(self, repo):
        """A stand-in for GitHub: a bare repository at the path github_url points to."""
        github = os.path.join(self.tmp.name, "github")
        source = os.path.join(self.tmp.name, "seed")
        os.makedirs(source, exist_ok=True)
        open(os.path.join(source, "project.godot"), "w").close()
        run = lambda *a, cwd=None: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t"] + list(a),
                                                  cwd=cwd, capture_output=True, text=True, check=True)
        run("init", "-q", cwd=source)
        run("add", "-A", cwd=source)
        run("commit", "-qm", "seed", cwd=source)
        run("clone", "-q", "--bare", source, os.path.join(github, repo + ".git"))
        text = open(self.config).read().replace("[visor]\n", "[visor]\n"
            f"repos = {self.tmp.name}/repos\ngithub_owner = someone\ngithub_url = {github}/{{repo}}.git\n", 1)
        with open(self.config, "w") as f:
            f.write(text)
        os.makedirs(os.path.join(self.tmp.name, "repos"), exist_ok=True)
        return dispatch.Dispatcher(self.config, self.state, now=lambda: self.clock,
                                   other_run_active=lambda: self.other_run, log=self.logged.append)

    def test_a_note_makes_a_folder_project_that_the_next_note_can_use(self):
        self.note("inbox", "new.md", "New project: College Class\nGitHub: no\nModel: glimmer\n")
        self.d.once()
        self.assertEqual(self.calls_made(), [], "making a project runs no model")
        self.assertTrue(os.path.isdir(os.path.join(self.tasks, "projects", "College Class")))
        reply = self.read("your-turn", "new.md")
        self.assertIn("## Project created", reply)
        self.assertIn("`Project: College Class`", reply)
        self.note("inbox", "ch2.md", "Project: College Class\nWrite: chapter two\n")
        self.d.once()
        (call,) = self.calls_made()
        self.assertIn("--folder", call)
        self.assertEqual(call[2], "glimmer-abliterated", "the model the project was made with")
        # A restarted dispatcher still knows it.
        again = dispatch.Dispatcher(self.config, self.state)
        self.assertIn("College Class", again.projects)

    def test_github_yes_clones_the_repository_of_that_name(self):
        d = self.make_github("someone/puzzle-game")
        self.note("inbox", "new.md", "New project: puzzle-game\nGitHub: yes\n")
        d.once()
        target = os.path.join(self.tmp.name, "repos", "puzzle-game")
        self.assertTrue(os.path.exists(os.path.join(target, "project.godot")))
        self.assertIn("Cloned `someone/puzzle-game`", self.read("your-turn", "new.md"))
        self.assertEqual(d.projects["puzzle-game"]["source"], target)

    def test_github_can_name_another_repository(self):
        d = self.make_github("friend/their-game")
        self.note("inbox", "new.md", "New project: theirs\nGitHub: friend/their-game\nTests: python3 tests.py\n")
        d.once()
        self.assertEqual(d.projects["theirs"]["tests"], "python3 tests.py")

    def test_a_project_that_cannot_be_made_comes_back_with_the_reason(self):
        d = self.make_github("someone/real")
        self.note("inbox", "a.md", "New project: game\n")
        self.note("inbox", "b.md", "New project: missing\nGitHub: yes\n")
        self.note("inbox", "c.md", "New project: x\nGitHub: perhaps\n")
        self.note("inbox", "d.md", "New project: y\nModel: nonesuch\n")
        for _ in range(4):
            d.once()
        self.assertIn("already a project called `game`", self.read("your-turn", "a.md"))
        self.assertIn("could not clone `someone/missing`", self.read("your-turn", "b.md"))
        self.assertIn("`GitHub: perhaps` is not one visor understands", self.read("your-turn", "c.md"))
        self.assertIn("`Model: nonesuch` is not one visor knows", self.read("your-turn", "d.md"))
        self.assertNotIn("missing", d.projects)

    # Folder projects

    def test_a_write_round_on_a_folder_writes_drafts_and_lists_them(self):
        self.note("inbox", "ch5.md", "Project: story\nModel: official\nWrite: chapter five\n")
        self.d.once()
        (call,) = self.calls_made()
        self.assertIn("--write", call)
        self.assertIn("--folder", call)
        self.assertNotIn("--continue", call)
        note = self.read("your-turn", "ch5.md")
        self.assertIn("## Drafts, ", note)
        self.assertIn("`Chapter4b.md` (draft of `Chapter4a.md`)", note)

    def test_thinking_yes_asks_a_model_that_can_think_to_think(self):
        self.note("inbox", "ch5.md", "Project: story\nThinking: yes\nWrite: five\n")
        self.d.once()
        (call,) = self.calls_made()
        self.assertEqual(call[call.index("--thinking") + 1], "medium")

    def test_thinking_takes_a_level_and_no_means_none(self):
        self.note("inbox", "a.md", "Project: story\nThinking: high\nWrite: five\n")
        self.note("inbox", "b.md", "Project: story\nThinking: No\nWrite: five\n")
        self.d.once()
        self.d.once()
        first, second = self.calls_made()
        self.assertEqual(first[first.index("--thinking") + 1], "high")
        self.assertNotIn("--thinking", second)

    def test_thinking_from_a_model_that_cannot_think_is_turned_back(self):
        self.note("inbox", "a.md", "Project: game\nModel: official\nThinking: yes\nPlan:\n")
        self.note("inbox", "b.md", "Project: story\nThinking: perhaps\nWrite: five\n")
        self.d.once()
        self.d.once()
        self.assertEqual(self.calls_made(), [])
        self.assertIn("official cannot think", self.read("your-turn", "a.md"))
        self.assertIn("`Thinking: perhaps` is not one visor knows", self.read("your-turn", "b.md"))

    def test_a_folder_project_s_own_model_is_used_when_the_note_names_none(self):
        self.note("inbox", "ch5.md", "Project: story\nWrite: chapter five\n")
        self.d.once()
        (call,) = self.calls_made()
        self.assertEqual(call[2], "glimmer-abliterated")

    def test_a_reply_on_a_folder_asks_for_more_writing_from_the_folder_as_it_is(self):
        self.note("approved", "ch5.md", "Project: story\nModel: official\nWrite: five\n\n---\n\n## Drafts, x\n\nDone.\n\n---\n\n## Your reply\n\nDarker.\n")
        self.d.once()
        (call,) = self.calls_made()
        self.assertIn("--write", call)
        self.assertNotIn("--continue", call, "the last round's drafts are already in the folder")
        self.assertIn("Darker.", call[-1])

    def test_a_paused_write_round_carries_on_its_own_branch(self):
        os.environ.update(FAKE_PAUSES="1", FAKE_SESSION="1")
        self.note("inbox", "ch5.md", "Project: story\nModel: official\nWrite: five\n")
        self.d.once()
        first, second = self.calls_made()
        self.assertEqual(second[second.index("--continue") + 1], "ch5-coder-official-pi-run0")
        self.assertEqual(second[second.index("--fork") + 1], "ch5-coder-official-pi-run0")

    def test_build_on_a_folder_and_write_on_a_repository_are_turned_back(self):
        self.note("inbox", "a.md", "Project: story\nBuild: five\n")
        self.note("inbox", "b.md", "Project: textgame\nWrite: five\n")
        self.d.once()
        self.d.once()
        self.assertEqual(self.calls_made(), [])
        self.assertIn("ask for `Write:`", self.read("your-turn", "a.md"))
        self.assertIn("`Write:` is for folder projects", self.read("your-turn", "b.md"))

    def test_a_project_needs_a_source_or_a_folder_but_not_both(self):
        text = open(self.config).read().replace("folder = ", "source = /x\nfolder = ")
        with open(self.config, "w") as f:
            f.write(text)
        with self.assertRaises(Exception):
            dispatch.Dispatcher(self.config, self.state)

    # Live logs

    def test_each_model_s_run_writes_a_live_log_linked_from_the_note(self):
        self.note("inbox", "task.md", "Project: game\nAnalysis: why?\n")
        self.d.once()
        official, abliterated = self.calls_made()
        logs = os.path.join(self.tasks, "logs")
        self.assertEqual(official[official.index("--live-log") + 1], os.path.join(logs, "task - official.md"))
        self.assertEqual(abliterated[abliterated.index("--live-log") + 1], os.path.join(logs, "task - abliterated.md"))
        note = self.read("your-turn", "task.md")
        self.assertIn("live log: [[task - official]]", note)
        self.assertIn("live log: [[task - abliterated]]", note)

    def test_a_running_round_shows_its_live_log_in_status(self):
        self.d.save_state({"note": "task.md", "runs": [], "last_build": {},
                           "running": {"model": "official", "round": "plan", "started": "now",
                                       "log": os.path.join(self.tasks, "logs", "task - official.md")}})
        self.d.write_status()
        self.assertIn("live log: [[task - official]]", self.read("", "STATUS.md"))

    # Long runs are paused and carried on with a fresh model

    def test_a_paused_part_carries_on_its_own_conversation_and_branch(self):
        os.environ.update(FAKE_PAUSES="2", FAKE_SESSION="1")
        self.note("inbox", "task.md", "Project: game\nModel: official\nBuild:\nMake the thing.\n")
        self.d.once()
        first, second, third = self.calls_made()
        self.assertNotIn("--fork", first)
        self.assertEqual(second[second.index("--fork") + 1], "task-coder-official-pi-run0")
        self.assertEqual(second[second.index("--continue") + 1], "task-coder-official-pi-run0")
        self.assertNotIn("--notes", second, "the task and the discussion are already in the conversation")
        self.assertEqual(third[third.index("--fork") + 1], "task-coder-official-pi-run1")
        self.assertIn("in 3 parts", self.read("your-turn", "task.md"))

    def test_a_paused_question_carries_on_its_conversation(self):
        os.environ.update(FAKE_PAUSES="1", FAKE_SESSION="1")
        self.note("inbox", "q.md", "Project: game\nModel: official\nAnalysis: why?\n")
        self.d.once()
        first, second = self.calls_made()
        self.assertEqual(second[second.index("--fork") + 1], "q-coder-official-pi-run0")
        self.assertNotIn("--continue", second, "a question has no branch to carry on")

    def test_a_harness_that_cannot_carry_on_a_conversation_is_told_where_it_got_to(self):
        os.environ.update(FAKE_PAUSES="1", FAKE_SESSION="1")
        text = open(self.config).read().replace("harness = pi", "harness = qwen")
        with open(self.config, "w") as f:
            f.write(text)
        d = dispatch.Dispatcher(self.config, self.state, now=lambda: self.clock,
                                other_run_active=lambda: self.other_run, log=self.logged.append)
        self.note("inbox", "q.md", "Project: game\nModel: official\nAnalysis: why?\n")
        d.once()
        first, second = self.calls_made()
        self.assertNotIn("--fork", second)
        self.assertIn("WHERE THE PREVIOUS PART GOT TO", second[-1])

    def test_a_paused_build_with_no_saved_conversation_is_told_where_it_got_to(self):
        os.environ["FAKE_PAUSES"] = "2"
        self.note("inbox", "task.md", "Project: game\nModel: official\nBuild:\nMake the thing.\n")
        self.d.once()
        calls = self.calls_made()
        self.assertEqual(len(calls), 3)
        self.assertNotIn("--continue", calls[0])
        self.assertEqual(calls[1][calls[1].index("--continue") + 1], "task-coder-official-pi-run0")
        self.assertEqual(calls[2][calls[2].index("--continue") + 1], "task-coder-official-pi-run1")
        second_notes = calls[1][-1]
        self.assertIn("WHERE THE PREVIOUS PART GOT TO", second_notes)
        self.assertIn("Summary from part 0.", second_notes)
        self.assertIn("Working on it, call 0.", second_notes)
        self.assertIn("Summary from part 1.", calls[2][-1], "each part hears from the one just before it")
        note = self.read("your-turn", "task.md")
        self.assertIn("in 3 parts", note)
        self.assertIn("pull request: https://example.invalid/pull/2", note, "only the last part opens one")
        self.assertEqual(note.count("## Build, "), 1, "one answer per model, however many parts")
        self.assertTrue(any("no conversation saved by task-coder-official-pi-run0" in m for m in self.logged))

    def test_a_paused_question_with_no_saved_conversation_is_asked_again_with_what_was_found(self):
        os.environ["FAKE_PAUSES"] = "1"
        self.note("inbox", "q.md", "Project: game\nModel: official\nAnalysis: why?\n")
        self.d.once()
        first, second = self.calls_made()
        self.assertIn("--analysis", second)
        self.assertNotIn("--continue", second, "a question has no branch to carry on")
        self.assertIn("Summary from part 0.", second[-1])

    def test_a_run_that_keeps_pausing_is_given_up_after_a_few_parts(self):
        os.environ["FAKE_PAUSES"] = "100"
        self.note("inbox", "task.md", "Project: game\nModel: official\nBuild:\n")
        self.d.once()
        self.assertEqual(len(self.calls_made()), dispatch.MAX_PARTS)
        note = self.read("your-turn", "task.md")
        self.assertIn(f"stopped carrying this on after {dispatch.MAX_PARTS} parts", note)
        self.assertIn("> **PAUSED", note)

    def test_a_part_paused_before_the_model_answered_is_not_carried_on_again(self):
        # Re-reading a long conversation can outlast a part; the next would do the same.
        os.environ.update(FAKE_PAUSES="100", FAKE_SESSION="1", FAKE_CALLS_MADE="0")
        self.note("inbox", "q.md", "Project: game\nModel: official\nAnalysis: why?\n")
        self.d.once()
        self.assertEqual(len(self.calls_made()), 1)
        self.assertIn("part 1 was paused before the model answered once", self.read("your-turn", "q.md"))

    def test_both_models_are_each_carried_on_separately(self):
        os.environ["FAKE_PAUSES"] = "1"
        self.note("inbox", "task.md", "Project: game\nBuild:\n")
        self.d.once()
        models = [c[2] for c in self.calls_made()]
        self.assertEqual(models, ["coder-official", "coder-official", "coder-abliterated"])

    def test_only_a_process_running_the_runner_counts_as_a_run(self):
        self.assertTrue(dispatch._is_runner("bash /home/x/ai-visor/gate/run_gate.sh /srv/code/p task.md m pi"))
        self.assertTrue(dispatch._is_runner("/usr/bin/bash ./gate/run_gate.sh a b c d"))
        self.assertFalse(dispatch._is_runner("sh -c until grep -q ok log; do sleep 60; done; "
                                             "while pgrep -f run_gate.sh; do sleep 60; done"))
        self.assertFalse(dispatch._is_runner("grep run_gate.sh notes.md"))
        self.assertFalse(dispatch._is_runner("bash /x/install.sh run_gate.sh"))

    def test_no_self_update_happens_under_a_run(self):
        self.other_run = True
        self.d.maybe_update()  # would need git and the network if it did not return at once

    # Reviews

    def reviewing(self):
        with open(self.config, "a") as f:
            f.write("\n")
        text = open(self.config).read().replace("self_update", "x").replace("[visor]\n", "[visor]\nreview = yes\n", 1)
        with open(self.config, "w") as f:
            f.write(text)
        return dispatch.Dispatcher(self.config, self.state, now=lambda: self.clock,
                                   other_run_active=lambda: self.other_run, log=self.logged.append)

    def test_each_build_is_reviewed_on_its_branch_with_the_checks_and_the_builders_claims(self):
        d = self.reviewing()
        self.note("inbox", "task.md", "Project: game\nModel: abliterated\nBuild:\nMake the thing.\n")
        d.once()
        build, review = self.calls_made()
        self.assertEqual(review[2], "coder-official", "the reviewer is the official model")
        self.assertIn("--analysis", review)
        self.assertEqual(review[review.index("--continue") + 1], "task-coder-abliterated-pi-run0")
        brief = review[-1]
        self.assertIn("REVIEW THIS BUILD", brief)
        self.assertIn("git diff abc123 HEAD", brief)
        self.assertIn("80 → 140", brief)
        self.assertIn("Answer from coder-abliterated.", brief, "the builder's claims, to be checked")
        note = self.read("your-turn", "task.md")
        self.assertIn("## Review of the abliterated build", note)
        self.assertIn("by the official model", note)
        self.assertLess(note.index("## Build, "), note.index("## Review of"))

    def test_a_build_from_before_the_checks_has_them_run_for_its_review(self):
        work = os.path.join(self.tmp.name, "work")
        repo = os.path.join(work, "old-build")
        os.makedirs(repo)
        git = lambda *a: subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t"] + list(a),
                                        capture_output=True, text=True, check=True).stdout
        git("init", "-q")
        open(os.path.join(repo, "speed.gd"), "w").write("var speed = 80\n")
        git("add", "-A"); git("commit", "-qm", "base")
        base = git("rev-parse", "HEAD").strip()
        open(os.path.join(repo, "speed.gd"), "w").write("var speed = 140\n")
        git("commit", "-qam", "faster")
        out = os.path.join(self.results, "old-build")
        os.makedirs(out)
        open(os.path.join(out, "report.md"), "w").write(f"# old-build\n\n- base: {base}\n\n## Pull request\n\nnone\n")
        text = open(self.config).read().replace("[visor]\n", f"[visor]\nwork = {work}\n", 1)
        with open(self.config, "w") as f:
            f.write(text)
        d = self.reviewing()
        task = os.path.join(self.tmp.name, "task.md")
        open(task, "w").write("Make it faster.\n")
        d.review_build("old-build", "game", task, "")
        (review,) = self.calls_made()
        self.assertIn("80 → 140", review[-1])

    def test_a_paused_build_is_reviewed_once_when_it_finishes(self):
        os.environ["FAKE_PAUSES"] = "1"
        d = self.reviewing()
        self.note("inbox", "task.md", "Project: game\nModel: official\nBuild:\n")
        d.once()
        rounds = ["review" if "--analysis" in c else "build" for c in self.calls_made()]
        self.assertEqual(rounds, ["build", "build", "review"])

    def test_questions_and_plans_are_not_reviewed(self):
        d = self.reviewing()
        self.note("inbox", "q.md", "Project: game\nModel: official\nAnalysis: why?\n")
        d.once()
        self.assertEqual(len(self.calls_made()), 1)

    # Mistakes in a note come back to the owner, loudly

    # The web

    def test_a_run_is_given_the_web_only_when_its_note_asks(self):
        self.note("inbox", "a.md", "Project: game\nModel: official\nWeb: yes\nAnalysis: what changed in Godot 4.5?\n")
        self.note("inbox", "b.md", "Project: game\nModel: official\nWeb: No\nAnalysis: and here?\n")
        self.note("inbox", "c.md", "Project: game\nModel: official\nAnalysis: and here?\n")
        for _ in range(3):
            self.d.once()
        asked, declined, silent = sorted(self.calls_made(), key=lambda call: call[1])
        self.assertIn("--web", asked)
        self.assertNotIn("--web", declined)
        self.assertNotIn("--web", silent)
        # Where it went is in the reply.
        self.assertIn("web: 2 connections through the VPN to 1 hosts: example.org", self.read("your-turn", "a.md"))
        self.assertNotIn("web:", self.read("your-turn", "c.md"))

    def test_every_part_of_a_paused_round_keeps_the_web(self):
        os.environ["FAKE_PAUSES"] = "1"
        self.note("inbox", "task.md", "Project: game\nModel: official\nWeb: yes\nAnalysis: look it up\n")
        self.d.once()
        first, second = self.calls_made()
        self.assertIn("--web", first)
        self.assertIn("--web", second)

    def test_with_the_vpn_down_a_note_that_asks_for_the_web_comes_back_unrun(self):
        self.vpn = False
        self.note("inbox", "a.md", "Project: game\nModel: official\nWeb: yes\nPlan:\n")
        self.note("inbox", "b.md", "Project: game\nModel: official\nPlan:\n")
        self.d.once()
        self.d.once()
        note = self.read("your-turn", "a.md")
        self.assertIn("## Visor could not start", note)
        self.assertIn("the VPN is not running on the server", note)
        # A note that asks for no web is unaffected.
        (call,) = self.calls_made()
        self.assertTrue(call[1].endswith("b.md"))
        self.assertNotIn("--web", call)

    def test_the_vpn_counts_as_up_only_while_its_proxy_answers(self):
        import socket
        proxy = socket.socket()
        proxy.bind(("127.0.0.1", 0))
        proxy.listen()
        self.d.vpn_proxy = f"127.0.0.1:{proxy.getsockname()[1]}"
        self.assertTrue(dispatch.Dispatcher.vpn_up(self.d))
        proxy.close()
        self.assertFalse(dispatch.Dispatcher.vpn_up(self.d))

    def test_a_web_line_visor_cannot_read_and_one_on_a_media_job_are_turned_back(self):
        self.note("inbox", "a.md", "Project: game\nWeb: perhaps\nPlan:\n")
        self.note("inbox", "b.md", "Web: yes\nImage: a lighthouse at dusk\n")
        self.d.once()
        self.d.once()
        self.assertEqual(self.calls_made(), [])
        self.assertIn("`Web: perhaps` is not one visor knows", self.read("your-turn", "a.md"))
        self.assertIn("images run with no network", self.read("your-turn", "b.md"))

    def test_a_run_that_stops_before_the_agent_starts_says_why_in_the_note(self):
        os.environ["FAKE_STOPS_EARLY"] = "WALL CHECK FAILED -- the agent was not started: net door: no way out"
        self.note("inbox", "task.md", "Project: game\nModel: official\nWeb: yes\nPlan:\n")
        self.d.once()
        self.assertIn("> **THE RUN STOPPED BEFORE THE AGENT STARTED (exit 1):** WALL CHECK FAILED -- the agent "
                      "was not started: net door: no way out", self.read("your-turn", "task.md"))

    def test_a_note_without_a_project_comes_back_with_the_reason(self):
        self.note("inbox", "task.md", "Make the thing.\n")
        self.d.once()
        self.assertEqual(self.calls_made(), [])
        note = self.read("your-turn", "task.md")
        self.assertIn("## Visor could not start", note)
        self.assertIn("`Project:`", note)

    def test_an_unknown_project_or_model_comes_back_with_the_reason(self):
        self.note("inbox", "a.md", "Project: nothing\n")
        self.note("inbox", "b.md", "Project: game\nModel: gpt\n")
        self.d.once()
        self.d.once()
        self.assertIn("Known: game, notests, story, textgame", self.read("your-turn", "a.md"))
        self.assertIn("`Model: gpt`", self.read("your-turn", "b.md"))

    def test_a_build_on_a_project_with_no_way_to_test_it_is_refused(self):
        self.note("inbox", "task.md", "Project: notests\nBuild:\n")
        self.d.once()
        self.assertEqual(self.calls_made(), [])
        self.assertIn("no test command", self.read("your-turn", "task.md"))

    def test_a_failed_run_is_said_plainly_at_the_top_of_its_section(self):
        os.environ["FAKE_EXIT"] = "1"
        self.note("inbox", "task.md", "Project: game\nModel: official\n")
        self.d.once()
        note = self.read("your-turn", "task.md")
        self.assertIn("> **AGENT FAILED (exit 1).**", note)
        self.assertIn("> Stopped as stuck", note)

    def test_a_crash_in_visor_hands_the_note_back_and_leaves_nothing_running(self):
        self.note("inbox", "task.md", "Project: game\nModel: official\n")
        self.d.runner = "/nonexistent/runner"
        message = self.d.once()
        self.assertIn("FAILED", message)
        self.assertIn("## Visor failed", self.read("your-turn", "task.md"))
        self.assertIsNone(self.d.load_state("task.md")["running"])

    # Upkeep

    def test_an_interrupted_run_is_reported_and_handed_back(self):
        self.note("working", "task.md", "Project: game\n")
        state = self.d.load_state("task.md")
        state["running"] = {"model": "official", "round": "build", "started": "earlier"}
        self.d.save_state(state)
        self.d.recover()
        self.assertIn("## Interrupted", self.read("your-turn", "task.md"))
        self.assertIsNone(self.d.load_state("task.md")["running"])

    def test_a_note_in_working_that_visor_did_not_start_is_left_alone(self):
        self.note("working", "manual.md", "Project: game\n")
        self.d.recover()
        self.d.once()
        self.assertTrue(os.path.exists(os.path.join(self.tasks, "working", "manual.md")))

    def test_the_sample_job_comes_back_when_it_is_removed(self):
        self.d.once()
        path = os.path.join(self.tasks, "sample-job.md")
        self.assertTrue(os.path.exists(path))
        os.remove(path)
        self.d.once()
        with open(path) as f:
            self.assertEqual(f.read(), self.d.sample_job())

    def test_the_sample_job_lists_what_this_server_has_and_follows_a_download(self):
        import zipfile
        tools = os.path.join(self.tmp.name, "tools")
        os.makedirs(os.path.join(tools, "models", "comfy", "checkpoints"))
        open(os.path.join(tools, "models", "comfy", "checkpoints", dispatch.media.IMAGE_MODELS["pony"]["file"]), "w").close()
        with zipfile.ZipFile(os.path.join(tools, "models", "voices-v1.0.bin"), "w") as z:
            for voice in ("af_heart", "bm_george", "jf_alpha"):
                z.writestr(voice + ".npy", b"")
        with open(self.config, "a") as f:
            f.write(f"\n[media]\ntools = {tools}\n")
        d = dispatch.Dispatcher(self.config, self.state, now=lambda: self.clock,
                                other_run_active=lambda: self.other_run, log=self.logged.append)
        d.ensure_sample()
        sample = self.read("", "sample-job.md")
        self.assertIn("| `glimmer` | Muse-Glimmer 30B", sample)
        self.assertIn("Can think: `Thinking: yes`.", sample)
        self.assertRegex(sample, r"\| `pony` \|[^\n]*\| yes \|")
        self.assertRegex(sample, r"\| `chroma` \|[^\n]*\| not downloaded \|")
        self.assertIn("- American English: `af_heart`", sample)
        self.assertIn("- Japanese: `jf_alpha`", sample)
        self.assertIn("Transcription is not installed on this server yet.", sample)
        # A model downloaded later shows up without anyone deleting the file.
        open(os.path.join(tools, "models", "comfy", "checkpoints", dispatch.media.IMAGE_MODELS["aom3"]["file"]), "w").close()
        d.ensure_sample()
        self.assertRegex(self.read("", "sample-job.md"), r"\| `aom3` \|[^\n]*\| yes \|")
        self.assertIn("updated sample-job.md", self.logged)

    def test_the_sample_job_is_itself_a_valid_note_for_a_known_project(self):
        header = dispatch.parse_header(self.d.sample_job())
        self.assertEqual(header["model"], "both")
        self.assertEqual(header["web"], "No")
        self.assertEqual(header["round"], "plan")
        self.assertEqual(header["project"], "game")
        self.assertIn("`game`, `notests`, `story`, `textgame`", self.d.sample_job())

    def test_the_sample_and_status_files_are_never_taken_as_tasks(self):
        self.d.once()
        self.d.write_status()
        self.assertEqual(self.d.queue(), [])
        self.assertEqual(self.calls_made(), [])

    def test_control_characters_never_reach_a_note(self):
        path = self.note("inbox", "task.md", "Project: game\n")
        dispatch.append_to_note(path, "## Answer\n\nfine\x00\x00 text\x1b[0m\n")
        text = self.read("inbox", "task.md")
        self.assertNotIn("\x00", text)
        self.assertNotIn("\x1b", text)
        self.assertIn("fine text[0m", text)

    def test_notes_stay_editable_by_the_owners_group(self):
        self.note("inbox", "task.md", "Project: game\nModel: official\n")
        self.d.once()
        mode = os.stat(os.path.join(self.tasks, "your-turn", "task.md")).st_mode
        self.assertTrue(mode & stat.S_IWGRP)

    def test_status_lists_what_waits_and_what_ran(self):
        self.note("inbox", "a.md", "Project: game\nModel: official\n")
        self.d.once()
        self.note("inbox", "b.md", "Project: game\n")
        self.clock = time.time() - 1000  # b has not settled, but still counts as waiting
        self.d.write_status()
        status = open(os.path.join(self.tasks, "STATUS.md")).read()
        self.assertIn("b.md (inbox)", status)
        self.assertIn("`a.md`, plan on official, exit 0", status)

    def test_missing_settings_say_where_they_should_be(self):
        with self.assertRaisesRegex(dispatch.ConfigError, "visor.conf.example"):
            dispatch.Dispatcher(os.path.join(self.tmp.name, "absent.conf"), self.state)


if __name__ == "__main__":
    unittest.main()
