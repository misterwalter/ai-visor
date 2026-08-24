"""visor — analyse a GitHub issue or PR against a local checkout."""
import argparse
import datetime as dt
import os
import subprocess
import sys
import time

from . import bundle, config, gh, llm, prompts, state

MIN_INTERVAL = 30  # seconds; below this we are just hammering GitHub


def _repo_root(path):
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             cwd=path, capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return os.path.abspath(path)


def _looks_like_godot(repo_root):
    if os.path.exists(os.path.join(repo_root, "project.godot")):
        return True
    try:
        return any(f.endswith(".gd") for f in os.listdir(repo_root))
    except OSError:
        return False


def _log(name, text):
    os.makedirs(config.LOG_DIR, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(config.LOG_DIR, f"{stamp}-{name}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def _say(msg, quiet=False):
    if not quiet:
        print(msg, file=sys.stderr)


# --- the one analysis path, shared by `analyze` and `loop` -------------------

def analyze_item(number, repo_root, repo=None, *, kind=None, model=None,
                 num_ctx=None, think=None, task=None, godot=False,
                 dry_run=False, post=False, quiet=False):
    """Fetch, assemble, call, optionally post.

    `kind` is "issue" or "pr" when the caller already knows — the loop always
    does, because it came from `issue list` or `pr list`. Only fall back to
    probing when a human passed a bare number.

    Returns a dict with the outcome, or None if nothing was produced.
    """
    pull_request = (kind == "pr") if kind else gh.is_pr(number, repo, repo_root)
    if pull_request:
        item = gh.fetch_pr(number, repo, repo_root)
        diff = gh.fetch_pr_diff(number, repo, repo_root)
    else:
        item = gh.fetch_issue(number, repo, repo_root)
        diff = None
    _say(f"{'PR' if pull_request else 'issue'} #{number}: {item.get('title', '')}", quiet)

    b = bundle.build(item, repo_root, diff=diff)
    _say(f"  context: {b.used} chars", quiet)
    for line in b.manifest:
        _say(f"    · {line}", quiet)

    use_godot = godot or _looks_like_godot(repo_root)
    messages = prompts.build_messages(b.text(), task=task, godot=use_godot)

    prompt_path = _log(f"{number}-prompt.md",
                       messages[0]["content"] + "\n\n" + messages[1]["content"])
    _say(f"  prompt logged: {prompt_path}", quiet)

    if dry_run:
        _say("  dry run — model not called", quiet)
        return {"item": item, "is_pr": pull_request, "posted": False,
                "body": None, "dry_run": True}

    _say(f"  calling {model or config.MODEL} ...", quiet)
    text, stats = llm.chat(messages, model=model, num_ctx=num_ctx, think=think)
    _say(f"  done in {stats['wall_seconds']}s "
         f"(prompt {stats['prompt_tokens']} tok, out {stats['output_tokens']} tok)", quiet)

    footer = (f"\n\n---\n*ai-visor · {stats['model']} · "
              f"{stats['prompt_tokens']} prompt tokens · {stats['wall_seconds']}s*")
    body = text.rstrip() + footer
    _log(f"{number}-response.md", body)

    posted = False
    if post:
        url = gh.post_comment(number, body, repo, repo_root, pull_request=pull_request)
        _say(f"  posted: {url}", quiet)
        posted = True

    return {"item": item, "is_pr": pull_request, "posted": posted,
            "body": body, "stats": stats, "dry_run": False}


def _current_updated_at(number, repo, repo_root, pull_request):
    """Re-read updatedAt after we act, so our own comment is not 'new activity'."""
    try:
        fresh = (gh.fetch_pr if pull_request else gh.fetch_issue)(number, repo, repo_root)
        return fresh.get("updatedAt"), fresh.get("headRefOid")
    except gh.GhError:
        return None, None


# --- commands ---------------------------------------------------------------

def cmd_analyze(args):
    repo_root = _repo_root(args.path)
    _say(f"repo: {repo_root}")
    if not gh.check_available():
        sys.exit("gh is unavailable or unauthenticated. Run: gh auth login")

    result = analyze_item(
        args.number, repo_root, args.repo, model=args.model, num_ctx=args.num_ctx,
        think=args.think, task=args.task, godot=args.godot,
        dry_run=args.dry_run, post=args.post,
    )
    if result and result.get("body"):
        print(result["body"])
        if not result["posted"]:
            _say("\n(not posted — pass --post to comment on GitHub)")
    # Keep state honest even for manual runs, so the loop does not redo this.
    if result and not result.get("dry_run"):
        updated, sha = _current_updated_at(args.number, args.repo, repo_root,
                                           result["is_pr"])
        state.record(args.repo, args.number, "pr" if result["is_pr"] else "issue",
                     updated, sha)


def cmd_loop(args):
    repo_root = _repo_root(args.path)
    if not gh.check_available():
        sys.exit("gh is unavailable or unauthenticated. Run: gh auth login")

    interval = max(args.interval, MIN_INTERVAL)
    me = gh.current_login(repo_root)
    target = args.repo or repo_root
    _say(f"watching {target} every {interval}s"
         + (f", label '{args.label}'" if args.label else ", all open items")
         + (" [POSTING]" if args.post else " [not posting]")
         + (f" as {me}" if me else ""))
    if not args.label:
        _say("note: no --label filter, so every open item is in scope")

    while True:
        try:
            cycle(args, repo_root, me)
        except KeyboardInterrupt:
            _say("\nstopped")
            return
        except gh.GhError as exc:
            _say(f"github error: {exc}; backing off")
            time.sleep(interval * 2)
            continue
        except llm.LlmError as exc:
            _say(f"model error: {exc}; backing off")
            time.sleep(interval * 2)
            continue

        if args.once:
            return
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            _say("\nstopped")
            return


def cycle(args, repo_root, me):
    """One pass: list, decide, analyse what changed."""
    st = state.load()
    items = list(gh.list_open(args.label, args.repo, repo_root, args.limit))
    items += list(gh.list_open_prs(args.label, args.repo, repo_root, args.limit))

    due = []
    for it in items:
        if it.get("isDraft") and args.skip_drafts:
            continue
        if state.needs_analysis(args.repo, it["number"], it.get("updatedAt"),
                                it.get("headRefOid"), state=st):
            due.append(it)

    if not due:
        _say(f"[{dt.datetime.now():%H:%M:%S}] {len(items)} open, nothing new")
        return

    _say(f"[{dt.datetime.now():%H:%M:%S}] {len(due)} of {len(items)} need attention")
    for it in due:
        number, kind = it["number"], it["kind"]
        try:
            result = analyze_item(
                number, repo_root, args.repo, kind=kind, model=args.model,
                num_ctx=args.num_ctx, think=args.think, task=args.task,
                godot=args.godot, dry_run=args.dry_run, post=args.post,
            )
        except (gh.GhError, llm.LlmError) as exc:
            _say(f"  #{number} failed: {exc}")
            continue

        if result is None or result.get("dry_run"):
            continue

        # Belt and braces against answering ourselves: if the newest comment is
        # ours and we have seen this item before, do not treat it as activity.
        if me and gh.last_comment_author(result["item"]) == me:
            _say(f"  #{number}: newest comment is ours")

        updated, sha = _current_updated_at(number, args.repo, repo_root,
                                           result["is_pr"])
        st = state.record(args.repo, number, kind, updated, sha, state=st)


def cmd_list(args):
    repo_root = _repo_root(args.path)
    st = state.load()
    rows = list(gh.list_open(args.label, args.repo, repo_root, args.limit))
    rows += list(gh.list_open_prs(args.label, args.repo, repo_root, args.limit))
    for it in rows:
        seen = state.seen(args.repo, it["number"], state=st)
        mark = " " if seen is None else ("*" if state.needs_analysis(
            args.repo, it["number"], it.get("updatedAt"), it.get("headRefOid"),
            state=st) else "=")
        labels = ",".join(l["name"] for l in it.get("labels", []))
        print(f"{mark} {it['kind']:<5} #{it['number']:<5} "
              f"{it['title'][:60]:<62} {labels}")
    print("\n  * needs analysis   = up to date   (blank) never seen",
          file=sys.stderr)


def cmd_models(args):
    names = llm.available_models()
    if not names:
        sys.exit(f"no models found at {config.OLLAMA_URL}")
    for n in names:
        print(("* " if n == config.MODEL else "  ") + n)


def cmd_forget(args):
    repo_root = _repo_root(args.path)
    if state.forget(args.repo, args.number):
        print(f"#{args.number} will be analysed again")
    else:
        print(f"#{args.number} was not in the state file")


def main(argv=None):
    p = argparse.ArgumentParser(prog="visor", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def shared(sp):
        sp.add_argument("--path", default=".", help="local checkout to analyse")
        sp.add_argument("--repo", help="owner/name, if not inferable")
        sp.add_argument("--model", help="override the analysis model")
        sp.add_argument("--num-ctx", type=int, help="override context window")
        sp.add_argument("--think", help="thinking level: off/low/medium/high")
        sp.add_argument("--task", help="override the analysis instruction")
        sp.add_argument("--godot", action="store_true", help="force Godot 4 guardrails")
        sp.add_argument("--dry-run", action="store_true",
                        help="assemble and log the prompt, but do not call the model")
        sp.add_argument("--post", action="store_true",
                        help="post the result as a GitHub comment")

    a = sub.add_parser("analyze", help="analyse one issue or PR")
    a.add_argument("number", type=int)
    shared(a)
    a.set_defaults(func=cmd_analyze)

    lo = sub.add_parser("loop", help="watch for new activity and analyse it")
    shared(lo)
    lo.add_argument("--interval", type=int, default=300,
                    help=f"seconds between passes (minimum {MIN_INTERVAL})")
    lo.add_argument("--label", help="only items carrying this label")
    lo.add_argument("--limit", type=int, default=20, help="items per listing")
    lo.add_argument("--once", action="store_true", help="a single pass, then exit")
    lo.add_argument("--skip-drafts", action="store_true", default=True,
                    help="ignore draft PRs")
    lo.set_defaults(func=cmd_loop)

    l = sub.add_parser("list", help="list open items and their analysis state")
    l.add_argument("--path", default=".")
    l.add_argument("--repo")
    l.add_argument("--label")
    l.add_argument("--limit", type=int, default=20)
    l.set_defaults(func=cmd_list)

    m = sub.add_parser("models", help="list models Ollama is offering")
    m.set_defaults(func=cmd_models)

    f = sub.add_parser("forget", help="drop an item from state so it re-analyses")
    f.add_argument("number", type=int)
    f.add_argument("--path", default=".")
    f.add_argument("--repo")
    f.set_defaults(func=cmd_forget)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
