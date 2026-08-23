"""visor — analyse a GitHub issue or PR against a local checkout."""
import argparse
import datetime as _dt
import os
import subprocess
import sys

from . import bundle, config, gh, llm, prompts


def _repo_root(path):
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             cwd=path, capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return os.path.abspath(path)


def _looks_like_godot(repo_root):
    return (os.path.exists(os.path.join(repo_root, "project.godot"))
            or any(f.endswith(".gd") for f in os.listdir(repo_root)))


def _log(name, text):
    os.makedirs(config.LOG_DIR, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(config.LOG_DIR, f"{stamp}-{name}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def cmd_analyze(args):
    repo_root = _repo_root(args.path)
    print(f"repo: {repo_root}", file=sys.stderr)

    if not gh.check_available():
        sys.exit("gh is not available or not authenticated. Run: gh auth login")

    pull_request = gh.is_pr(args.number, args.repo, repo_root)
    if pull_request:
        item = gh.fetch_pr(args.number, args.repo, repo_root)
        diff = gh.fetch_pr_diff(args.number, args.repo, repo_root)
        print(f"loaded PR #{args.number}: {item['title']}", file=sys.stderr)
    else:
        item = gh.fetch_issue(args.number, args.repo, repo_root)
        diff = None
        print(f"loaded issue #{args.number}: {item['title']}", file=sys.stderr)

    b = bundle.build(item, repo_root, diff=diff)
    print(f"context: {b.used} chars", file=sys.stderr)
    for line in b.manifest:
        print(f"  · {line}", file=sys.stderr)

    godot = args.godot or _looks_like_godot(repo_root)
    messages = prompts.build_messages(b.text(), task=args.task, godot=godot)

    prompt_log = _log(f"issue-{args.number}-prompt.md",
                      messages[0]["content"] + "\n\n" + messages[1]["content"])
    print(f"prompt logged: {prompt_log}", file=sys.stderr)

    if args.dry_run:
        print("dry run — not calling the model", file=sys.stderr)
        return

    print(f"calling {args.model or config.MODEL} (this is the slow part) ...", file=sys.stderr)
    text, stats = llm.chat(messages, model=args.model, num_ctx=args.num_ctx,
                           think=args.think)
    print(f"done in {stats['wall_seconds']}s "
          f"(prompt {stats['prompt_tokens']} tok, out {stats['output_tokens']} tok)",
          file=sys.stderr)

    footer = (f"\n\n---\n*ai-visor · {stats['model']} · "
              f"{stats['prompt_tokens']} prompt tokens · {stats['wall_seconds']}s*")
    body = text.rstrip() + footer

    _log(f"issue-{args.number}-response.md", body)
    print(body)

    if args.post:
        url = gh.post_comment(args.number, body, args.repo, repo_root,
                              pull_request=pull_request)
        print(f"posted: {url}", file=sys.stderr)
    else:
        print("\n(not posted — pass --post to comment on GitHub)", file=sys.stderr)


def cmd_list(args):
    repo_root = _repo_root(args.path)
    for it in gh.list_open(args.label, args.repo, repo_root):
        labels = ",".join(l["name"] for l in it.get("labels", []))
        print(f"#{it['number']:<5} {it['title'][:70]:<72} {labels}")


def cmd_models(args):
    names = llm.available_models()
    if not names:
        sys.exit(f"no models found at {config.OLLAMA_URL}")
    for n in names:
        print(("* " if n == config.MODEL else "  ") + n)


def main(argv=None):
    p = argparse.ArgumentParser(prog="visor", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="analyse an issue or PR")
    a.add_argument("number", type=int)
    a.add_argument("--path", default=".", help="local checkout to analyse")
    a.add_argument("--repo", help="owner/name, if not inferable from the checkout")
    a.add_argument("--model", help="override the analysis model")
    a.add_argument("--num-ctx", type=int, help="override context window")
    a.add_argument("--think", help="thinking level: off/low/medium/high")
    a.add_argument("--task", help="override the analysis instruction")
    a.add_argument("--godot", action="store_true", help="force Godot 4 guardrails")
    a.add_argument("--dry-run", action="store_true",
                   help="assemble and log the prompt, but do not call the model")
    a.add_argument("--post", action="store_true", help="post the result as a comment")
    a.set_defaults(func=cmd_analyze)

    l = sub.add_parser("list", help="list open issues")
    l.add_argument("--path", default=".")
    l.add_argument("--repo")
    l.add_argument("--label")
    l.set_defaults(func=cmd_list)

    m = sub.add_parser("models", help="list models Ollama is offering")
    m.set_defaults(func=cmd_models)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
