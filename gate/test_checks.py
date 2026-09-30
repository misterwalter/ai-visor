#!/usr/bin/env python3
"""Tests for checks.py, each against a small throwaway git repository whose
changes copy a fault found by hand in a real build.

    python3 gate/test_checks.py
"""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import checks  # noqa: E402


class ChecksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        self.git("init", "-q")
        self.write("scripts/catalog.gd", 'func build():\n\t_stat("move_speed", 80.0, 0.045)\n\t_stat("mess", 35.0, 0.035)\n')
        self.write("scripts/world.gd", "func make_vignette():\n\treturn 1\n\nfunc build():\n\tmake_vignette()\n")
        self.write("test/unit/test_world.gd", "func test_vignette():\n\tassert_eq(make_vignette(), 1)\n")
        self.commit("base")
        self.base = self.git("rev-parse", "HEAD").strip()

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@t"] + list(args),
                              capture_output=True, text=True, check=True).stdout

    def write(self, path, text):
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def found(self):
        return checks.check(self.repo, self.base)

    def test_a_value_changed_to_suit_a_test_is_flagged_with_both_numbers(self):
        # A base speed went from 80 to 140 so that the agent's own test would pass.
        self.write("scripts/catalog.gd", 'func build():\n\t_stat("move_speed", 140.0, 0.045)\n\t_stat("mess", 35.0, 0.035)\n')
        self.commit("faster")
        (item,) = self.found()["numbers changed"]
        self.assertIn("scripts/catalog.gd:2", item)
        self.assertIn("80.0, 0.045 → 140.0, 0.045", item)

    def test_code_nothing_calls_is_flagged(self):
        # The first background build added a class that nothing ever used.
        self.write("scripts/fancy.gd", "class_name FancyBackground\n\nfunc set_speed(v):\n\tpass\n")
        self.commit("unused")
        unused = " ".join(self.found()["nothing refers to it"])
        self.assertIn("FancyBackground", unused)
        self.assertIn("set_speed", unused)

    def test_a_mention_in_a_comment_is_not_a_use(self):
        # The class that nothing used named itself in its own usage example.
        self.write("scripts/fancy.gd", "class_name FancyBackground\n## var n = FancyBackground.new()\n")
        self.commit("commented")
        self.assertIn("FancyBackground", " ".join(self.found()["nothing refers to it"]))

    def test_code_that_is_called_is_not_flagged(self):
        self.write("scripts/fancy.gd", "func make_fancy():\n\treturn 2\n")
        self.write("scripts/world.gd", "func make_vignette():\n\treturn 1\n\nfunc build():\n\tmake_vignette()\n\tmake_fancy()\n")
        self.commit("used")
        self.assertEqual(self.found()["nothing refers to it"], [])

    def test_a_deleted_function_is_flagged(self):
        # Another build removed the existing vignette to make room.
        self.write("scripts/world.gd", "func build():\n\tpass\n")
        self.commit("removed")
        self.assertIn("`make_vignette` from `scripts/world.gd`", self.found()["removed"])

    def test_a_deleted_check_is_flagged(self):
        self.write("test/unit/test_world.gd", "func test_vignette():\n\tpass\n")
        self.commit("weaker")
        (item,) = self.found()["test lines removed"]
        self.assertIn("assert_eq(make_vignette(), 1)", item)

    def test_no_test_added_is_said_and_a_new_test_clears_it(self):
        self.write("scripts/world.gd", "func make_vignette():\n\treturn 2\n\nfunc build():\n\tmake_vignette()\n")
        self.commit("change")
        self.assertEqual(len(self.found()["no test added"]), 1)
        self.write("test/unit/test_world.gd", "func test_vignette():\n\tassert_eq(make_vignette(), 1)\n\nfunc test_two():\n\tassert_eq(2, 2)\n")
        self.commit("tested")
        self.assertEqual(self.found()["no test added"], [])

    def test_a_python_project_is_read_the_same_way(self):
        self.write("engine/rules.py", "def score(x):\n    return x * 3\n")
        self.write("tests.py", "def test_score():\n    check(score(1) == 3)\n")
        self.commit("python")
        found = self.found()
        self.assertEqual(found["nothing refers to it"], [], "score is used by its test")
        self.assertEqual(found["no test added"], [])

    def test_a_clean_change_reports_only_what_is_true(self):
        self.write("test/unit/test_world.gd", "func test_vignette():\n\tassert_eq(make_vignette(), 1)\n\nfunc test_more():\n\tassert_eq(1, 1)\n")
        self.commit("only a test")
        self.assertEqual(checks.render(self.found()), "- nothing found")

    def test_a_bad_base_says_so_instead_of_failing_the_run(self):
        from io import StringIO
        out, sys.stdout = sys.stdout, StringIO()
        try:
            self.assertEqual(checks.main(["checks.py", self.repo, "no-such-commit"]), 0)
            printed = sys.stdout.getvalue()
        finally:
            sys.stdout = out
        self.assertIn("checks could not run", printed)


if __name__ == "__main__":
    unittest.main()
