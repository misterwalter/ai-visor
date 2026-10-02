#!/usr/bin/env python3
"""Tests for livelog.py, on events shaped like pi's own.

    python3 gate/test_livelog.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import livelog  # noqa: E402

WORK = "/srv/code/work/run1"


def text(*pieces):
    events = [{"type": "message_update", "assistantMessageEvent": {"type": "text_start"}}]
    events += [{"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": p}} for p in pieces]
    return events + [{"type": "message_update", "assistantMessageEvent": {"type": "text_end"}}]


def tool(name, args, result, error=False):
    return [{"type": "tool_execution_start", "toolCallId": "1", "toolName": name, "args": args},
            {"type": "tool_execution_end", "toolCallId": "1", "toolName": name, "isError": error,
             "result": {"content": [{"type": "text", "text": result}]}}]


def render(events):
    r = livelog.Renderer(WORK)
    return "".join(r.event(e) for e in events)


class RenderTest(unittest.TestCase):
    def test_streamed_text_is_joined(self):
        self.assertIn("Let me read the board.", render(text("Let me ", "read the ", "board.")))

    def test_a_shell_command_is_a_callout_with_the_command_and_its_result(self):
        out = render(tool("bash", {"command": f"grep -n work_order {WORK}/scripts/board.gd"}, "495:func work_order"))
        self.assertIn("> [!example]- bash: grep -n work_order scripts/board.gd", out)
        self.assertIn("> 495:func work_order", out)
        self.assertNotIn(WORK, out, "paths are shown inside the project")

    def test_an_edit_is_shown_as_a_diff(self):
        out = render(tool("edit", {"path": f"{WORK}/a.gd", "edits": [{"oldText": "speed = 80", "newText": "speed = 140"}]}, "ok"))
        self.assertIn("edit: a.gd (1 change)", out)
        self.assertIn("> -speed = 80", out)
        self.assertIn("> +speed = 140", out)

    def test_tools_run_at_once_are_each_shown_with_their_own_result(self):
        # pi starts both, then the second finishes first.
        a, b = tool("read", {"path": "a.gd"}, "from a"), tool("grep", {"pattern": "x", "path": "."}, "from b")
        for e, call in ((a, "1"), (b, "2")):
            for event in e:
                event["toolCallId"] = call
        out = render([a[0], b[0], b[1], a[1]])
        self.assertLess(out.index("grep: x ."), out.index("from b"))
        self.assertLess(out.index("from b"), out.index("read: a.gd"))
        self.assertLess(out.index("read: a.gd"), out.index("from a"))

    def test_a_failed_tool_says_error(self):
        self.assertIn("**Error**", render(tool("edit", {"path": "a"}, "Could not find edits[6]", error=True)))

    def test_a_long_result_is_cut_and_says_so(self):
        out = render(tool("read", {"path": f"{WORK}/big.gd"}, "\n".join(f"line {i}" for i in range(100))))
        self.assertIn("line 29", out)
        self.assertNotIn("line 30\n", out)
        self.assertIn("70 more lines in the full record", out)

    def test_backticks_in_a_result_cannot_break_out_of_its_block(self):
        out = render(tool("read", {"path": "notes.md"}, "```\n# not a heading\n```"))
        self.assertIn("> ````", out)

    def test_thinking_is_a_collapsed_callout_on_every_line(self):
        events = [{"type": "message_update", "assistantMessageEvent": {"type": "thinking_start"}},
                  {"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "delta": "first\nsecond"}},
                  {"type": "message_update", "assistantMessageEvent": {"type": "thinking_end"}}] + text("Answer.")
        out = render(events)
        self.assertIn("> [!quote]- Thinking\n> first\n> second", out)
        self.assertIn("\n\n\nAnswer.", out, "the answer is outside the callout")

    def test_a_turn_ends_with_the_time_and_tokens(self):
        out = render([{"type": "turn_end", "message": {"timestamp": 0, "usage": {"input": 25514, "output": 41}}}])
        self.assertIn("read 25,514 tokens, wrote 41", out)

    def test_a_summary_is_logged(self):
        out = render([{"type": "compaction_start"}, {"type": "compaction_end", "result": {"summary": "Read board.gd."}}])
        self.assertIn("Summarising the conversation", out)
        self.assertIn("> Read board.gd.", out)

    def test_control_characters_are_dropped(self):
        self.assertNotIn("\x00", render(text("a\x00b")))


class FollowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.events = os.path.join(self.tmp.name, "agent-output.jsonl")
        self.log = os.path.join(self.tmp.name, "logs", "task-official.md")
        os.makedirs(os.path.dirname(self.log))

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, events, partial=""):
        with open(self.events, "a") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
            f.write(partial)

    def test_it_appends_as_the_run_goes_and_flushes_when_stopped(self):
        livelog.main(["livelog.py", "note", self.log, "## Analysis round, official"])
        self.write(text("Reading."))
        follower = subprocess.Popen([sys.executable, os.path.join(HERE, "livelog.py"), "follow",
                                     self.events, self.log, "--work", WORK, "--every", "0.5"])
        try:
            time.sleep(1.5)
            first = open(self.log).read()
            self.assertIn("## Analysis round, official", first)
            self.assertIn("Reading.", first)
            # Half a line: the harness is still writing it. It must wait for the rest.
            half = json.dumps(text("Second.")[1])
            self.write([], half[:20])
            time.sleep(1.2)
            self.assertNotIn("Second.", open(self.log).read())
            with open(self.events, "a") as f:
                f.write(half[20:] + "\n")
        finally:
            follower.terminate()
            follower.wait(timeout=10)
        final = open(self.log).read()
        self.assertTrue(final.startswith(first), "it only ever appends")
        self.assertIn("Second.", final)
        self.assertEqual(final.count("Reading."), 1)

    def test_a_large_log_carries_on_in_a_new_file(self):
        old = livelog.LOG_LIMIT
        livelog.LOG_LIMIT = 100
        try:
            livelog.append(self.log, "x" * 200)
            written = livelog.append(self.log, "more")
        finally:
            livelog.LOG_LIMIT = old
        self.assertTrue(written.endswith("task-official 2.md"))
        self.assertIn("Continued in [[task-official 2]]", open(self.log).read())
        self.assertEqual(open(written).read(), "more")


if __name__ == "__main__":
    unittest.main()
