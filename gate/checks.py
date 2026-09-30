#!/usr/bin/env python3
"""Mechanical checks on a build: things a reviewer should look at, found by reading
the diff rather than by trusting the agent's own report.

    checks.py <workspace> <base commit>

Prints a markdown list, one line per finding, for the run's report. Each is a
pointer for a person, not a verdict: a changed number may be exactly what was
asked for. Every build so far had at least one of these, and in each the
agent's own report said nothing about it.

    - test lines removed         a check or a test deleted from a test file
    - numbers changed            a line kept its shape and changed a value
    - nothing refers to it       a new function or class that nothing calls
    - removed                    a function or class deleted outright
    - no test added              the build added no test function
"""

import re
import subprocess
import sys

TEST_PATH = re.compile(r"(^|/)(tests?|spec)(/|$)|(^|/)test_[^/]*$|(^|/)[^/]*_test\.\w+$|(^|/)tests\.py$")
TEST_LINE = re.compile(r"\b(assert\w*|check|expect)\s*\(|\b(func|def)\s+test_")
TEST_DEF = re.compile(r"^\s*(static\s+)?(func|def)\s+test_\w+")
DEFINITION = re.compile(r"^\s*(?:static\s+)?(?:func|def)\s+(\w+)|\bclass_name\s+(\w+)|^\s*class\s+(\w+)")
NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")
MAX_LISTED = 8


def git(workspace, *args):
    return subprocess.run(["git", "-C", workspace] + list(args), capture_output=True, text=True, check=True).stdout


def parse_diff(diff):
    """Hunks as (path, [(new line number or None, sign, text)])."""
    hunks, path, lines, new_no = [], None, None, 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else None
            continue
        if raw.startswith("--- ") or raw.startswith("diff ") or raw.startswith("index "):
            continue
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if m:
            lines = []
            hunks.append((path, lines))
            new_no = int(m.group(1))
            continue
        if lines is None or not raw or raw[0] not in "+- ":
            continue
        sign, text = raw[0], raw[1:]
        if sign == "+":
            lines.append((new_no, "+", text))
            new_no += 1
        elif sign == "-":
            lines.append((None, "-", text))
        else:
            new_no += 1
    return hunks


def definitions(text):
    m = DEFINITION.search(text)
    if not m:
        return None
    return next(g for g in m.groups() if g)


def is_code(path):
    return bool(path) and re.search(r"\.(gd|py|gdshader|js|ts|cs|rs|go|c|cpp|h)$", path)


def check(workspace, base):
    diff = git(workspace, "diff", "--unified=0", "--no-color", base, "HEAD")
    hunks = parse_diff(diff)
    findings = {"test lines removed": [], "numbers changed": [], "nothing refers to it": [],
                "removed": [], "no test added": []}

    added_defs, removed_defs, test_added = {}, {}, False
    for path, lines in hunks:
        if path is None:
            continue
        test_file = bool(TEST_PATH.search(path))
        removed = [t for n, s, t in lines if s == "-"]
        added = [(n, t) for n, s, t in lines if s == "+"]
        if test_file:
            for text in removed:
                if TEST_LINE.search(text) and text.strip() not in {t.strip() for _, t in added}:
                    findings["test lines removed"].append(f"`{path}`: `{text.strip()[:90]}`")
            if any(TEST_DEF.search(t) for _, t in added):
                test_added = True
        if not is_code(path):
            continue
        # A line that kept its shape but changed a number, removed and added in one hunk.
        pending = list(removed)
        for n, text in added:
            shape = NUMBER.sub("#", text.strip())
            for old in pending:
                if NUMBER.sub("#", old.strip()) == shape and old.strip() != text.strip() and NUMBER.search(text):
                    before = ", ".join(NUMBER.findall(old))
                    after = ", ".join(NUMBER.findall(text))
                    findings["numbers changed"].append(f"`{path}:{n}`: {before} → {after}")
                    pending.remove(old)
                    break
        for text in removed:
            name = definitions(text)
            if name:
                removed_defs.setdefault(name, path)
        for n, text in added:
            name = definitions(text)
            if name:
                added_defs.setdefault(name, f"{path}:{n}")

    for name, path in removed_defs.items():
        if name not in added_defs:
            findings["removed"].append(f"`{name}` from `{path}`")

    for name, where in added_defs.items():
        # Engine callbacks and private helpers start with an underscore; tests are found by name.
        if name.startswith("_") or name.startswith("test_"):
            continue
        uses = git(workspace, "grep", "-n", "-w", "-I", name, "HEAD").splitlines()
        others = []
        for use in uses:
            text = use.split(":", 3)[-1] if use.count(":") >= 3 else use
            # A mention in a comment, often the class's own usage example, calls nothing.
            code = re.split(r"#|//", text, maxsplit=1)[0]
            if re.search(rf"\b{re.escape(name)}\b", code) and not DEFINITION.search(code):
                others.append(use)
        if not others:
            findings["nothing refers to it"].append(f"`{name}`, defined at `{where}`")

    if not test_added:
        findings["no test added"].append("the build added no test function")
    return findings


def render(findings):
    lines = []
    for label, items in findings.items():
        for item in items[:MAX_LISTED]:
            lines.append(f"- **{label}:** {item}")
        if len(items) > MAX_LISTED:
            lines.append(f"- **{label}:** and {len(items) - MAX_LISTED} more")
    return "\n".join(lines) or "- nothing found"


def main(argv):
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        print(render(check(argv[1], argv[2])))
    except subprocess.CalledProcessError as error:
        print(f"- **checks could not run:** {error.stderr.strip()[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
