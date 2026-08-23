"""Turn an issue into a pile of relevant code.

This module is the product. The model's only job is to read what this
assembles; every decision about *what* is relevant is made here, in ordinary
deterministic Python, because a local model asked to choose files gets paths
wrong and each wrong guess costs minutes.
"""
import os
import re
import subprocess

from . import config

# --- reference extraction ----------------------------------------------------

# Godot: res://scripts/player.gd:42 @ _physics_process()
RE_GODOT_RES = re.compile(r"res://([\w./\-]+\.\w+)(?::(\d+))?")
# Python: File "/path/to/thing.py", line 123
RE_PY_TRACE = re.compile(r'File "([^"]+)", line (\d+)')
# Generic: some/path/file.ext:123
RE_PATH_LINE = re.compile(r"([\w./\-]+\.[A-Za-z0-9]{1,5}):(\d+)")
# Bare filenames mentioned in prose or backticks
RE_BARE_FILE = re.compile(r"[`'\"]?([\w\-/]+\.(?:gd|py|cs|cpp|h|tscn|tres|json|toml|ya?ml))[`'\"]?")
# Identifiers worth grepping for: CamelCase, snake_case, or backticked
RE_BACKTICKED = re.compile(r"`([A-Za-z_][\w.]{2,})`")
RE_CAMEL = re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b")
RE_SNAKE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+){1,})\b")

# Words that look like identifiers but are just English or noise.
STOPWORDS = {
    "github_com", "http_s", "self_hosted", "make_sure", "such_as",
    "for_example", "as_well", "note_that",
}


def extract_references(text):
    """(paths_with_lines, bare_paths) mentioned anywhere in the text."""
    with_lines, bare = [], set()
    for m in RE_GODOT_RES.finditer(text):
        with_lines.append((m.group(1), int(m.group(2)) if m.group(2) else None))
    for m in RE_PY_TRACE.finditer(text):
        with_lines.append((m.group(1), int(m.group(2))))
    for m in RE_PATH_LINE.finditer(text):
        with_lines.append((m.group(1), int(m.group(2))))
    for m in RE_BARE_FILE.finditer(text):
        bare.add(m.group(1))
    for path, _ in with_lines:
        bare.discard(path)
    return with_lines, sorted(bare)


def extract_symbols(text, limit=12):
    """Identifiers worth grepping the repo for."""
    found = []
    for regex in (RE_BACKTICKED, RE_CAMEL, RE_SNAKE):
        for m in regex.finditer(text):
            s = m.group(1)
            if s.lower() in STOPWORDS or len(s) < 4 or "." in s:
                continue
            if s not in found:
                found.append(s)
    return found[:limit]


# --- repo lookup -------------------------------------------------------------

def resolve(path, repo_root):
    """Map a mentioned path onto a real file, tolerating partial paths."""
    direct = os.path.join(repo_root, path)
    if os.path.isfile(direct):
        return direct
    if os.path.isabs(path) and os.path.isfile(path):
        return path
    # Fall back to matching on basename, preferring the shortest path.
    target = os.path.basename(path)
    matches = []
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in config.SKIP_DIRS]
        if target in files:
            matches.append(os.path.join(root, target))
    matches.sort(key=len)
    return matches[0] if matches else None


def read_excerpt(path, line=None):
    """Whole file if small, otherwise a window around the interesting line."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError as exc:
        return f"[could not read: {exc}]"
    total = len(lines)
    text = "".join(lines)
    if len(text) <= config.MAX_FILE_CHARS and line is None:
        return _numbered(lines, 1)
    if line is None:
        head = _numbered(lines[:200], 1)
        return head + f"\n[... truncated, {total} lines total ...]"
    lo = max(0, line - config.TRACE_CONTEXT_LINES)
    hi = min(total, line + config.TRACE_CONTEXT_LINES)
    body = _numbered(lines[lo:hi], lo + 1)
    return f"[lines {lo + 1}-{hi} of {total}]\n{body}"


def _numbered(lines, start):
    return "".join(f"{i:>5} | {ln}" for i, ln in enumerate(lines, start=start))


def grep(symbol, repo_root, limit=12):
    """Where does this identifier appear? Uses git grep for speed and sanity."""
    try:
        out = subprocess.run(
            ["git", "grep", "-n", "--no-color", "-F", symbol],
            cwd=repo_root, capture_output=True, text=True, timeout=20,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    hits = [ln for ln in out.splitlines() if ln.strip()]
    return hits[:limit]


def repo_tree(repo_root, limit=120):
    """A shallow map, so the model knows what exists without reading it all."""
    entries = []
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = sorted(d for d in dirs if d not in config.SKIP_DIRS)
        rel = os.path.relpath(root, repo_root)
        for f in sorted(files):
            if f.endswith(config.SOURCE_SUFFIXES):
                entries.append(os.path.normpath(os.path.join(rel, f)))
            if len(entries) >= limit:
                return entries, True
    return entries, False
