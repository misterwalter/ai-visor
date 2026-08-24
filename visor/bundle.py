"""Assemble the context bundle sent to the model, within a character budget."""
import os

from . import config, context


class Bundle:
    def __init__(self):
        self.sections = []
        self.manifest = []   # human-readable record of what went in
        self.used = 0
        self.attached_files = 0

    def add(self, title, body, note=None):
        if not body:
            return False
        block = f"\n===== {title} =====\n{body.rstrip()}\n"
        if self.used + len(block) > config.MAX_CONTEXT_CHARS:
            self.manifest.append(f"SKIPPED (budget) {title}")
            return False
        self.sections.append(block)
        self.used += len(block)
        if title.startswith("FILE "):
            self.attached_files += 1
        self.manifest.append(note or title)
        return True

    def text(self):
        return "".join(self.sections)


def build(item, repo_root, diff=None):
    """item is the dict from gh.fetch_issue / fetch_pr."""
    b = Bundle()
    body = item.get("body") or ""
    comments = item.get("comments") or []
    discussion = "\n\n".join(
        f"--- comment by {c.get('author', {}).get('login', '?')} ---\n{c.get('body', '')}"
        for c in comments
    )
    searchable = "\n".join([item.get("title", ""), body, discussion])

    b.add(f"ISSUE #{item.get('number')}: {item.get('title', '')}",
          f"State: {item.get('state')}   URL: {item.get('url')}\n\n{body}",
          note=f"issue #{item.get('number')} body")

    if discussion:
        b.add("DISCUSSION", discussion, note=f"{len(comments)} comment(s)")

    if diff:
        trimmed = diff
        if len(trimmed) > config.MAX_FILE_CHARS * 2:
            trimmed = trimmed[: config.MAX_FILE_CHARS * 2] + "\n[... diff truncated ...]"
        b.add("PULL REQUEST DIFF", trimmed, note="PR diff")

    # Files named with a line number get priority — a stack trace is the single
    # most valuable thing in a bug report.
    with_lines, bare = context.extract_references(searchable)
    seen = set()
    for path, line in with_lines:
        real = context.resolve(path, repo_root)
        if not real or (real, line) in seen:
            continue
        seen.add((real, line))
        rel = os.path.relpath(real, repo_root)
        b.add(f"FILE {rel} (around line {line})",
              context.read_excerpt(real, line),
              note=f"traced: {rel}:{line}")

    for path in bare:
        real = context.resolve(path, repo_root)
        if not real or any(real == r for r, _ in seen):
            continue
        seen.add((real, None))
        rel = os.path.relpath(real, repo_root)
        b.add(f"FILE {rel}", context.read_excerpt(real), note=f"mentioned: {rel}")

    symbols = context.extract_symbols(searchable)
    grep_blocks = []
    for sym in symbols:
        hits = context.grep(sym, repo_root)
        if hits:
            grep_blocks.append(f"# {sym}\n" + "\n".join(hits))
    if grep_blocks:
        b.add("SYMBOL SEARCH", "\n\n".join(grep_blocks),
              note=f"grepped {len(grep_blocks)} symbol(s)")

    # Prose-only reports attach nothing above. Send the source instead: for a
    # small repo that is affordable, and it is the difference between the model
    # guessing and the model reading.
    if b.attached_files < config.FALLBACK_WHEN_FEWER_THAN:
        added = _attach_by_relevance(b, repo_root, searchable, seen)
        if added:
            b.manifest.append(f"thin context -> attached {added} source file(s)")

    entries, truncated = context.repo_tree(repo_root)
    tree = "\n".join(entries) + ("\n[... truncated ...]" if truncated else "")
    b.add("REPOSITORY FILES", tree, note=f"{len(entries)} paths")

    return b


def _attach_by_relevance(b, repo_root, text, seen):
    """Add source files, most plausibly relevant first, until the budget runs out.

    Ranking is deliberately crude: a file whose name is mentioned in the report
    wins, then smaller files, because several small files usually inform an
    analysis better than one large one.
    """
    lowered = text.lower()
    candidates = []
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in config.SKIP_DIRS]
        for name in files:
            if not name.endswith(config.SOURCE_SUFFIXES):
                continue
            full = os.path.join(root, name)
            if any(full == r for r, _ in seen):
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            stem = os.path.splitext(name)[0].lower()
            mentioned = 0 if (stem in lowered and len(stem) > 3) else 1
            candidates.append((mentioned, size, full))

    candidates.sort()
    added = 0
    for _, _, full in candidates[: config.FALLBACK_MAX_FILES]:
        rel = os.path.relpath(full, repo_root)
        if not b.add(f"FILE {rel}", context.read_excerpt(full), note=f"source: {rel}"):
            break
        added += 1
    return added
