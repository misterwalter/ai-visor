#!/usr/bin/env python3
"""Tests for folders.py, on a throwaway folder shaped like a notes folder.

    python3 gate/test_folders.py
"""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import folders  # noqa: E402


class FoldersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        folders.HISTORIES = os.path.join(root, "histories")
        self.folder = os.path.join(root, "vault", "My Story")
        self.write(self.folder, "Chapter4a.md", "The owner's chapter four.\n")
        self.write(self.folder, "Story Bible.md", "Mara has green eyes.\n")
        self.write(self.folder, ".obsidian/workspace.json", "{}\n")
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, base, rel, text):
        path = os.path.join(base, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def read(self, rel):
        with open(os.path.join(self.folder, rel)) as f:
            return f.read()

    def git(self, *args, cwd):
        return folders.git(*args, cwd=cwd)

    def run_writes(self, name, changes, start_from=None):
        """A run as run_gate.sh makes it: a clone of the history, the agent's changes, a commit."""
        repo = folders.snapshot(self.folder)
        work = os.path.join(self.root, "work", name)
        subprocess.run(["git", "clone", "--quiet", repo, work], check=True)
        if start_from:
            self.git("fetch", "--quiet", start_from, f"visor/{os.path.basename(start_from)}", cwd=work)
            self.git("checkout", "--quiet", "-b", f"visor/{name}", "FETCH_HEAD", cwd=work)
        else:
            self.git("checkout", "--quiet", "-b", f"visor/{name}", "origin/main", cwd=work)
        for rel, text in changes.items():
            if text is None:
                os.remove(os.path.join(work, rel))
            else:
                self.write(work, rel, text)
        self.git("add", "-A", cwd=work)
        self.git("commit", "--quiet", "-m", "agent", cwd=work)
        return work

    def test_the_history_is_kept_outside_the_folder_and_leaves_hidden_files_out(self):
        repo = folders.snapshot(self.folder)
        self.assertFalse(repo.startswith(self.folder))
        self.assertFalse(os.path.exists(os.path.join(self.folder, ".git")), "nothing is added to the folder")
        tracked = self.git("--git-dir", repo, "ls-tree", "-r", "--name-only", "main", cwd=self.root).splitlines()
        self.assertEqual(sorted(tracked), ["Chapter4a.md", "Story Bible.md"])

    def test_the_owner_s_edits_are_recorded_and_an_unchanged_folder_adds_nothing(self):
        repo = folders.snapshot(self.folder)
        count = lambda: int(self.git("--git-dir", repo, "rev-list", "--count", "main", cwd=self.root))
        self.assertEqual(count(), 1)
        folders.snapshot(self.folder)
        self.assertEqual(count(), 1)
        self.write(self.folder, "Chapter4a.md", "The owner's chapter four, revised.\n")
        folders.snapshot(self.folder)
        self.assertEqual(count(), 2)

    def test_a_new_file_is_copied_and_a_changed_one_becomes_the_next_draft(self):
        work = self.run_writes("run1", {"Chapter5a.md": "Chapter five.\n",
                                        "Chapter4a.md": "The bot's chapter four.\n",
                                        "Story Bible.md": None})
        lines = folders.deliver(work, self.folder)
        self.assertEqual(self.read("Chapter5a.md"), "Chapter five.\n")
        self.assertEqual(self.read("Chapter4a.md"), "The owner's chapter four.\n", "the original is untouched")
        self.assertEqual(self.read("Chapter4b.md"), "The bot's chapter four.\n")
        self.assertEqual(self.read("Story Bible.md"), "Mara has green eyes.\n", "a deletion is not carried out")
        text = "\n".join(lines)
        self.assertIn("`Chapter4b.md` (draft of `Chapter4a.md`)", text)
        self.assertIn("left alone: `Story Bible.md`", text)

    def test_a_new_numbered_piece_is_delivered_as_its_first_draft(self):
        work = self.run_writes("run1", {"Chapter5.md": "Five.\n", "Notes on Mara.md": "Notes.\n"})
        lines = "\n".join(folders.deliver(work, self.folder))
        self.assertEqual(self.read("Chapter5a.md"), "Five.\n")
        self.assertFalse(os.path.exists(os.path.join(self.folder, "Chapter5.md")))
        self.assertEqual(self.read("Notes on Mara.md"), "Notes.\n", "a name without a number keeps its name")
        self.assertIn("`Chapter5a.md` (new, named as a first draft)", lines)

    def test_a_name_the_owner_took_meanwhile_is_not_overwritten(self):
        work = self.run_writes("run1", {"Chapter5a.md": "The bot's five.\n"})
        self.write(self.folder, "Chapter5a.md", "The owner's own five.\n")
        folders.deliver(work, self.folder)
        self.assertEqual(self.read("Chapter5a.md"), "The owner's own five.\n")
        self.assertEqual(self.read("Chapter5b.md"), "The bot's five.\n")

    def test_draft_names(self):
        self.write(self.folder, "Chapter4b.md", "taken")
        self.assertEqual(folders.next_draft(self.folder, "Chapter4a.md"), "Chapter4c.md")
        self.assertEqual(folders.next_draft(self.folder, "Chapter9.md"), "Chapter9a.md")
        self.assertEqual(folders.next_draft(self.folder, "Story Bible.md"), "Story Bible a.md")
        self.assertEqual(folders.next_draft(self.folder, "Part 2/Chapter1a.md"), os.path.join("Part 2", "Chapter1b.md"))
        self.assertEqual(folders.next_draft(self.folder, "Chapter4a.md", taken={"Chapter4c.md"}), "Chapter4d.md")
        self.assertIsNone(folders.next_draft(self.folder, "Chapter4z.md"))

    def test_a_run_in_parts_delivers_everything_its_parts_wrote(self):
        first = self.run_writes("part1", {"Chapter5a.md": "Half of five.\n"})
        self.write(self.folder, "Chapter4a.md", "The owner edited four between the parts.\n")
        second = self.run_writes("part2", {"Chapter5a.md": "All of five.\n", "Chapter6a.md": "Six.\n"}, start_from=first)
        folders.deliver(second, self.folder)
        self.assertEqual(self.read("Chapter5a.md"), "All of five.\n")
        self.assertEqual(self.read("Chapter6a.md"), "Six.\n")
        self.assertEqual(self.read("Chapter4a.md"), "The owner edited four between the parts.\n")
        self.assertFalse(os.path.exists(os.path.join(self.folder, "Chapter4b.md")),
                         "the owner's own edit is not mistaken for the bot's")


if __name__ == "__main__":
    unittest.main()
