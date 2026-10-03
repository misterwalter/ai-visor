#!/usr/bin/env python3
"""Projects that are a plain folder, such as one synced as notes, rather than a repository.

    folders.py history FOLDER         print where visor keeps the folder's history
    folders.py snapshot FOLDER        record the folder as it is now in that history
    folders.py deliver WORK FOLDER    copy what a run wrote back into the folder, as drafts

Visor keeps the folder's history itself, in a git repository outside the folder,
so nothing is added to the folder and nothing extra is synced. A run clones that
history like any repository, and its commits go back into it on a branch of
their own.

Delivery never overwrites. A file the run added is copied to the folder, with a
draft letter if its name ends in a number ("Chapter5.md" becomes "Chapter5a.md");
one it changed is copied as the next draft beside the original, "Chapter4a.md"
becoming "Chapter4b.md"; one it deleted is left alone. Prints one line per file, for the
run's report.
"""

import os
import re
import shutil
import string
import subprocess
import sys
import time

HISTORIES = os.environ.get("VISOR_FOLDERS", "/srv/code/folders")
# Hidden files and folders are the notes app's own (.obsidian, .trash), never the work.
EXCLUDE = ".*\n"


def git(*args, cwd=None, check=True):
    return subprocess.run(["git", "-c", "user.name=visor", "-c", "user.email=visor@localhost"] + list(args),
                          cwd=cwd, capture_output=True, text=True, check=check).stdout


def history(folder):
    """One history per folder, named after its path so that two folders never share one."""
    folder = os.path.realpath(folder)
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", folder.strip("/"))
    return os.path.join(HISTORIES, name + ".git")


def snapshot(folder):
    """Commit the folder as it is, the owner's own edits included. Returns the history's path."""
    folder = os.path.realpath(folder)
    if not os.path.isdir(folder):
        raise SystemExit(f"no such folder: {folder}")
    repo = history(folder)
    at = ["--git-dir", repo, "--work-tree", folder]
    if not os.path.isdir(repo):
        os.makedirs(HISTORIES, exist_ok=True)
        git("init", "--quiet", "--bare", "--initial-branch", "main", repo)
        git("--git-dir", repo, "config", "core.bare", "false")
        with open(os.path.join(repo, "info", "exclude"), "w") as f:
            f.write(EXCLUDE)
    git(*at, "add", "--all")
    # An empty folder is still a starting point: without a first commit there is no
    # branch for a run to begin from, and the run stops before the agent starts.
    no_commits = git(*at, "rev-parse", "--verify", "--quiet", "HEAD", check=False) == ""
    if no_commits or git(*at, "status", "--porcelain"):
        git(*at, "commit", "--quiet", "--allow-empty", "-m",
            f"The folder as it was at {time.strftime('%Y-%m-%d %H:%M')}")
    return repo


DRAFT = re.compile(r"^(.*\d)([a-z])$")


def next_draft(folder, rel, taken=()):
    """The first free draft name for rel: Chapter4a.md -> Chapter4b.md, Chapter4.md -> Chapter4a.md,
    Notes.md -> Notes a.md. "Free" means neither in the folder nor already chosen in this delivery."""
    directory, filename = os.path.split(rel)
    stem, ext = os.path.splitext(filename)
    m = DRAFT.match(stem)
    base, start = (m.group(1), string.ascii_lowercase.index(m.group(2)) + 1) if m else (stem, 0)
    joiner = "" if base[-1:].isdigit() else " "
    for letter in string.ascii_lowercase[start:]:
        candidate = os.path.join(directory, f"{base}{joiner}{letter}{ext}")
        if not os.path.exists(os.path.join(folder, candidate)) and candidate not in taken:
            return candidate
    return None


def deliver(work, folder):
    """Copy what the run changed, since the folder snapshot it started from, into the folder."""
    folder = os.path.realpath(folder)
    # Everything the run wrote, across all its parts: from where its branch left main.
    base = git("merge-base", "HEAD", "origin/main", cwd=work).strip()
    changes = git("diff", "--name-status", "--no-renames", base, "HEAD", cwd=work).splitlines()
    lines, taken = [], set()
    for change in changes:
        status, rel = change.split("\t", 1)
        source = os.path.join(work, rel)
        if status == "D":
            lines.append(f"- left alone: `{rel}`, which the run deleted")
            continue
        stem = os.path.splitext(os.path.basename(rel))[0]
        if status == "A" and stem[-1:].isdigit():
            # A new piece is a first draft, and a draft carries a letter: Chapter5.md is Chapter5a.md.
            target, note = next_draft(folder, rel, taken), "new, named as a first draft"
            if target is None:
                lines.append(f"- NOT DELIVERED: `{rel}`: no draft letter left after z")
                continue
        elif status == "A" and not os.path.exists(os.path.join(folder, rel)):
            target, note = rel, "new"
        else:
            target = next_draft(folder, rel, taken)
            if target is None:
                lines.append(f"- NOT DELIVERED: `{rel}`: no draft letter left after z")
                continue
            note = f"draft of `{rel}`" if status == "M" else f"`{rel}` was taken meanwhile"
        taken.add(target)
        os.makedirs(os.path.dirname(os.path.join(folder, target)) or folder, exist_ok=True)
        shutil.copyfile(source, os.path.join(folder, target))
        os.chmod(os.path.join(folder, target), 0o664)
        lines.append(f"- `{target}` ({note})")
    return lines


def main(argv):
    if len(argv) == 3 and argv[1] == "history":
        print(history(argv[2]))
    elif len(argv) == 3 and argv[1] == "snapshot":
        print(snapshot(argv[2]))
    elif len(argv) == 4 and argv[1] == "deliver":
        print("\n".join(deliver(argv[2], argv[3])) or "- nothing: the run changed no files")
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
