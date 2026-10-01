#!/usr/bin/env python3
"""Tests for dispatch.py, against a throwaway notes folder and a stand-in for
run_gate.sh that records how it was called and writes a result. No model, no
Godot, no network.

    python3 gate/test_dispatch.py
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

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
    build = "--analysis" not in args and "--plan-only" not in args
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
    report += ["- base: abc123", ""]
    if build:
        report += ["## Checks", "", "- **numbers changed:** `x.gd:3`: 80 → 140", ""]
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
                """))
        os.environ.update(FAKE_RESULTS=self.results, FAKE_CALLS=self.calls)
        os.environ.pop("FAKE_EXIT", None)
        os.environ.pop("FAKE_PAUSES", None)
        os.environ.pop("FAKE_SESSION", None)
        os.environ.pop("FAKE_CALLS_MADE", None)
        self.state = os.path.join(root, "state")
        self.other_run = False
        self.logged = []
        self.clock = time.time() + 1000  # every note has settled unless a test says otherwise
        self.d = dispatch.Dispatcher(self.config, self.state, now=lambda: self.clock,
                                     other_run_active=lambda: self.other_run, log=self.logged.append)

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
        self.assertEqual(dispatch.parse_header(text),
                         {"project": "my-game", "model": None, "round": "analysis"})

    def test_header_keys_are_case_blind_and_the_first_round_word_wins(self):
        text = "project: game\nMODEL: Official\nbuild:\nPlan: not this\n"
        self.assertEqual(dispatch.parse_header(text), {"project": "game", "model": "Official", "round": "build"})

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
        self.assertIn("Known: game, notests, textgame", self.read("your-turn", "a.md"))
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

    def test_the_sample_job_is_itself_a_valid_note_for_a_known_project(self):
        header = dispatch.parse_header(self.d.sample_job())
        self.assertEqual(header["model"], "both")
        self.assertEqual(header["round"], "plan")
        self.assertEqual(header["project"], "game")
        self.assertIn("`game`, `notests`, `textgame`", self.d.sample_job())

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
