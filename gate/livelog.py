#!/usr/bin/env python3
"""A live, readable log of an agent's work, written into the notes folder as it goes.

    livelog.py follow EVENTS LOG [--work DIR] [--every SECONDS]
    livelog.py note LOG TEXT

`follow` reads the harness's event stream (pi's --mode json output) as it grows
and appends what is new to LOG, a markdown file, every SECONDS (default 60): the
model's text as it streams, its thinking, each tool call with its arguments and
the start of its result, and the harness's summaries. It only ever appends, in
one write per batch, so a synced notes folder sees one small change a minute.
It runs until it is stopped, and writes what is left when it is.

`note` appends TEXT to LOG, for the runner's own headings and outcomes.

A log that passes LOG_LIMIT bytes carries on in a new file, "<name> 2.md", and
the old one ends with a link to it.
"""

import json
import os
import re
import signal
import sys
import time

RESULT_LINES = 30      # lines of a tool's result shown; the full record stays in gate-results
ARGUMENT_LINES = 40    # lines of a command, an edit or a written file shown
LINE_WIDTH = 300
LOG_LIMIT = 5_000_000
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean(text):
    return CONTROL.sub("", text)


def fence(text, lang=""):
    """A code block that the text cannot end early, however many backticks it holds."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text}\n{ticks}"


def shorten(text, lines, width=LINE_WIDTH):
    all_lines = text.rstrip("\n").split("\n")
    shown = [line if len(line) <= width else line[:width] + " …" for line in all_lines[:lines]]
    if len(all_lines) > lines:
        shown.append(f"… {len(all_lines) - lines} more lines in the full record")
    return "\n".join(shown)


def quoted(text):
    """Text inside a callout: every line starts with "> "."""
    return "\n".join("> " + line if line else ">" for line in text.split("\n"))


def stamp(ms=None):
    return time.strftime("%H:%M", time.localtime(ms / 1000 if ms else time.time()))


class Renderer:
    """Turns pi's events into markdown, one event at a time. Streamed text arrives in
    small pieces; the renderer keeps track of whether it is inside a thinking callout."""

    def __init__(self, work=""):
        self.work = work.rstrip("/") + "/" if work else ""
        self.in_thinking = False
        self.at_line_start = True
        # pi may run several tools at once, and they finish in any order. Each is
        # written whole when it finishes, from the arguments it started with.
        self.started = {}

    def rel(self, path):
        path = str(path)
        if self.work and path.startswith(self.work):
            return path[len(self.work):]
        return path

    def _end_thinking(self):
        if not self.in_thinking:
            return ""
        self.in_thinking = False
        self.at_line_start = True
        return "\n\n"

    def event(self, ev):
        kind = ev.get("type")
        if kind == "message_update":
            return self._stream(ev.get("assistantMessageEvent") or {})
        if kind == "tool_execution_start":
            self.started[ev.get("toolCallId")] = ev.get("args") or {}
            return ""
        if kind == "tool_execution_end":
            args = self.started.pop(ev.get("toolCallId"), {})
            return self._end_thinking() + self._tool_start(ev.get("toolName", "?"), args) + self._tool_end(ev)
        if kind == "turn_end":
            return self._end_thinking() + self._turn_end(ev.get("message") or {})
        if kind == "compaction_start":
            return self._end_thinking() + "\n\n> [!info] Summarising the conversation to make room\n\n"
        if kind == "compaction_end":
            summary = ((ev.get("result") or {}).get("summary") or "").strip()
            if not summary:
                return ""
            return "> [!info]- The summary it carries on with\n" + quoted(clean(summary)) + "\n\n"
        return ""

    def _stream(self, m):
        kind = m.get("type", "")
        if kind == "text_start":
            return self._end_thinking() + "\n"
        if kind == "text_delta":
            return clean(m.get("delta", ""))
        if kind == "thinking_start":
            self.in_thinking = True
            self.at_line_start = False
            return "\n\n> [!quote]- Thinking\n> "
        if kind == "thinking_delta" and self.in_thinking:
            delta = clean(m.get("delta", ""))
            return delta.replace("\n", "\n> ")
        if kind == "thinking_end":
            return self._end_thinking()
        return ""

    def _tool_start(self, name, args):
        if name == "bash":
            command = str(args.get("command", ""))
            title = command.strip().split("\n")[0]
            body = fence(shorten(command, ARGUMENT_LINES), "bash")
        elif name == "read":
            where = ""
            if args.get("offset") or args.get("limit"):
                where = f" (from line {args.get('offset', 1)}, {args.get('limit', 'all')} lines)"
            title = self.rel(args.get("path", "")) + where
            body = ""
        elif name == "edit":
            edits = args.get("edits") or [args]
            title = f"{self.rel(args.get('path', ''))} ({len(edits)} change{'s' if len(edits) != 1 else ''})"
            diffs = []
            for e in edits:
                old = str(e.get("oldText", "")).split("\n")
                new = str(e.get("newText", "")).split("\n")
                diffs.append("\n".join(["-" + line for line in old] + ["+" + line for line in new]))
            body = fence(shorten("\n…\n".join(diffs), ARGUMENT_LINES), "diff")
        elif name == "write":
            content = str(args.get("content", ""))
            title = f"{self.rel(args.get('path', ''))} ({content.count(chr(10)) + 1} lines)"
            body = fence(shorten(content, ARGUMENT_LINES))
        else:
            pattern = args.get("pattern") or args.get("query") or ""
            title = " ".join(str(x) for x in (pattern, self.rel(args.get("path", ""))) if x)
            body = fence(shorten(json.dumps(args, indent=1, ensure_ascii=False), ARGUMENT_LINES), "json")
        title = clean(title)[:150].replace("\n", " ")
        head = f"\n\n> [!example]- {name}: {title}\n"
        return self.rel_all(head + (quoted(body) + "\n" if body else ""))

    def _tool_end(self, ev):
        content = (ev.get("result") or {}).get("content") or []
        text = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        label = "**Error**" if ev.get("isError") else "Result"
        if not text.strip():
            return quoted(f"{label}: (nothing)") + "\n\n"
        shown = self.rel_all(shorten(clean(text), RESULT_LINES))
        return quoted(f"{label}:\n" + fence(shown)) + "\n\n"

    def rel_all(self, text):
        """Paths inside the project, shown from its top folder."""
        if not self.work:
            return text
        return text.replace(self.work, "").replace(self.work.rstrip("/"), ".")

    def _turn_end(self, message):
        usage = message.get("usage") or {}
        bits = [stamp(message.get("timestamp"))]
        if usage.get("input"):
            bits.append(f"read {usage['input']:,} tokens, wrote {usage.get('output', 0):,}")
        if message.get("stopReason") == "error":
            error = clean(str(message.get("errorMessage", "the model call failed")))
            return f"\n\n> [!failure] {error[:500]}\n\n"
        return f"\n\n*{' · '.join(bits)}*\n"


def append(log, text):
    """Append to the log, moving on to a new file once it is large. Returns the file written."""
    if not text:
        return log
    if os.path.exists(log) and os.path.getsize(log) > LOG_LIMIT:
        base, ext = os.path.splitext(log)
        m = re.match(r"^(.*) (\d+)$", base)
        nxt = f"{m.group(1)} {int(m.group(2)) + 1}{ext}" if m else f"{base} 2{ext}"
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"\n\nContinued in [[{os.path.splitext(os.path.basename(nxt))[0]}]].\n")
        log = nxt
    new = not os.path.exists(log)
    with open(log, "a", encoding="utf-8") as f:
        f.write(text)
    if new:
        os.chmod(log, 0o664)
    return log


def follow(events, log, work="", every=60.0):
    renderer = Renderer(work)
    position = 0
    state = {"log": log, "stop": False}
    signal.signal(signal.SIGTERM, lambda *_: state.update(stop=True))
    signal.signal(signal.SIGINT, lambda *_: state.update(stop=True))
    while True:
        stopping = state["stop"]
        out = []
        try:
            with open(events, "rb") as f:
                f.seek(position)
                chunk = f.read()
        except FileNotFoundError:
            chunk = b""
        # Only whole lines: the harness may be part-way through writing one.
        end = chunk.rfind(b"\n") + 1
        position += end
        for line in chunk[:end].decode("utf-8", errors="replace").splitlines():
            try:
                out.append(renderer.event(json.loads(line)))
            except ValueError:
                continue
        state["log"] = append(state["log"], "".join(out))
        if stopping:
            state["log"] = append(state["log"], renderer._end_thinking())
            return 0
        deadline = time.time() + every
        while time.time() < deadline and not state["stop"]:
            time.sleep(0.5)


def main(argv):
    if len(argv) >= 4 and argv[1] == "note":
        append(argv[2], argv[3].rstrip("\n") + "\n")
        return 0
    if len(argv) >= 4 and argv[1] == "follow":
        args, work, every = argv[4:], "", 60.0
        while args:
            if args[0] == "--work" and len(args) > 1:
                work, args = args[1], args[2:]
            elif args[0] == "--every" and len(args) > 1:
                every, args = float(args[1]), args[2:]
            else:
                print(f"unknown option: {args[0]}", file=sys.stderr)
                return 2
        return follow(argv[2], argv[3], work, every)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
