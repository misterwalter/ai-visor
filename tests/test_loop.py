"""Loop logic, with GitHub and the model mocked out.

The behaviour that matters here is not "does it call the model" but "does it
call it exactly when there is new activity, and never in response to itself".
"""
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["VISOR_STATE_FILE"] = os.path.join(tempfile.mkdtemp(), "state.json")

from visor import cli, gh, llm, state  # noqa: E402

REPO = "misterwalter/ai-visor"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def args(**over):
    base = dict(path=ROOT, repo=REPO, model=None, num_ctx=None, think=None,
                task=None, godot=False, dry_run=False, post=True,
                interval=30, label=None, limit=20, once=True, skip_drafts=True)
    base.update(over)
    return types.SimpleNamespace(**base)


class LoopTests(unittest.TestCase):
    def setUp(self):
        if os.path.exists(state.STATE_FILE):
            os.remove(state.STATE_FILE)
        self.posted = []
        self.analyzed = []
        self.issues = [{"number": 1, "title": "Crash on load", "kind": "issue",
                        "updatedAt": "2026-08-18T10:00:00Z", "labels": []}]
        self.prs = [{"number": 2, "title": "Fix loader", "kind": "pr",
                     "updatedAt": "2026-08-18T10:00:00Z", "labels": [],
                     "headRefOid": "aaa111", "isDraft": False}]

    def _item(self, number, is_pr):
        return {"number": number, "title": "t", "body": "b", "state": "OPEN",
                "url": "u", "comments": [], "updatedAt": self._updated(number),
                "headRefOid": "aaa111" if is_pr else None}

    def _updated(self, number):
        for src in (self.issues, self.prs):
            for it in src:
                if it["number"] == number:
                    return it["updatedAt"]
        return None

    def run_cycle(self, a=None):
        a = a or args()
        self.analyzed = []
        with mock.patch.object(gh, "list_open", side_effect=lambda *x, **k: iter(self.issues)), \
             mock.patch.object(gh, "list_open_prs", side_effect=lambda *x, **k: iter(self.prs)), \
             mock.patch.object(gh, "is_pr", side_effect=lambda n, *x, **k: n == 2), \
             mock.patch.object(gh, "fetch_issue", side_effect=lambda n, *x, **k: self._item(n, False)), \
             mock.patch.object(gh, "fetch_pr", side_effect=lambda n, *x, **k: self._item(n, True)), \
             mock.patch.object(gh, "fetch_pr_diff", return_value="--- a\n+++ b\n"), \
             mock.patch.object(gh, "post_comment", side_effect=lambda n, b, *x, **k: self.posted.append(n) or "url"), \
             mock.patch.object(llm, "chat", side_effect=self._fake_chat):
            cli.cycle(a, ROOT, "misterwalter")
        return self.analyzed

    def _fake_chat(self, messages, **kw):
        self.analyzed.append(len(messages[1]["content"]))
        return "## What I see\nnothing\n", {"wall_seconds": 1, "prompt_tokens": 10,
                                            "output_tokens": 5, "model": "fake"}

    def test_first_pass_analyses_everything(self):
        self.assertEqual(len(self.run_cycle()), 2)
        self.assertEqual(sorted(self.posted), [1, 2])

    def test_second_pass_is_quiet(self):
        self.run_cycle()
        self.posted.clear()
        self.assertEqual(len(self.run_cycle()), 0, "re-analysed with no new activity")
        self.assertEqual(self.posted, [], "posted again with nothing new")

    def test_new_comment_retriggers(self):
        self.run_cycle()
        self.issues[0]["updatedAt"] = "2026-08-18T11:00:00Z"
        self.assertEqual(len(self.run_cycle()), 1)

    def test_force_push_retriggers(self):
        self.run_cycle()
        self.prs[0]["headRefOid"] = "bbb222"
        self.assertEqual(len(self.run_cycle()), 1)

    def test_dry_run_does_not_post_or_record(self):
        self.run_cycle(args(dry_run=True))
        self.assertEqual(self.posted, [])
        # nothing recorded, so a real pass still has work to do
        self.assertEqual(len(self.run_cycle()), 2)

    def test_draft_prs_skipped(self):
        self.prs[0]["isDraft"] = True
        self.assertEqual(len(self.run_cycle()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
