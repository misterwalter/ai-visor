"""GitHub access via the `gh` CLI.

Using `gh` rather than the REST API so authentication is whatever the user has
already set up, and so posting is a visible, auditable shell command.
"""
import json
import subprocess


class GhError(RuntimeError):
    pass


def _gh(args, repo=None, cwd=None):
    cmd = ["gh"] + args
    if repo:
        cmd += ["--repo", repo]
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GhError(f"gh {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def check_available():
    try:
        _gh(["auth", "status"])
        return True
    except (GhError, FileNotFoundError):
        return False


def fetch_issue(number, repo=None, cwd=None):
    """Issue or PR with its comments. `gh issue view` works for both.

    Note `gh --json` takes a flat field list; sub-selection such as
    `comments(createdAt)` is not supported. Each comment already carries its
    own createdAt, and the item's updatedAt is what the loop actually keys on.
    """
    fields = ("number,title,body,author,labels,state,createdAt,updatedAt,"
              "url,comments")
    raw = _gh(["issue", "view", str(number), "--json", fields], repo, cwd)
    return json.loads(raw)


def fetch_pr(number, repo=None, cwd=None):
    fields = ("number,title,body,author,labels,state,createdAt,updatedAt,url,"
              "comments,files,additions,deletions,baseRefName,headRefName,"
              "headRefOid,isDraft")
    raw = _gh(["pr", "view", str(number), "--json", fields], repo, cwd)
    return json.loads(raw)


def fetch_pr_diff(number, repo=None, cwd=None):
    return _gh(["pr", "diff", str(number)], repo, cwd)


def is_pr(number, repo=None, cwd=None):
    try:
        _gh(["pr", "view", str(number), "--json", "number"], repo, cwd)
        return True
    except GhError:
        return False


def post_comment(number, body, repo=None, cwd=None, pull_request=False):
    kind = "pr" if pull_request else "issue"
    proc = subprocess.run(
        ["gh", kind, "comment", str(number), "--body-file", "-"]
        + (["--repo", repo] if repo else []),
        cwd=cwd, input=body, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise GhError(f"posting comment failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def list_open(label=None, repo=None, cwd=None, limit=20):
    """Open issues (gh excludes PRs here). Includes updatedAt for the loop."""
    args = ["issue", "list", "--state", "open", "--limit", str(limit),
            "--json", "number,title,labels,updatedAt,author"]
    if label:
        args += ["--label", label]
    for it in json.loads(_gh(args, repo, cwd)):
        it["kind"] = "issue"
        yield it


def list_open_prs(label=None, repo=None, cwd=None, limit=20):
    """Open PRs, with the head SHA so force-pushes are detectable."""
    args = ["pr", "list", "--state", "open", "--limit", str(limit),
            "--json", "number,title,labels,updatedAt,author,headRefOid,isDraft"]
    if label:
        args += ["--label", label]
    for it in json.loads(_gh(args, repo, cwd)):
        it["kind"] = "pr"
        yield it


def last_comment_author(item):
    """Login of whoever commented most recently, or None."""
    comments = item.get("comments") or []
    if not comments:
        return None
    return (comments[-1].get("author") or {}).get("login")


def current_login(cwd=None):
    """Who `gh` is authenticated as — so we can recognise our own comments."""
    try:
        raw = _gh(["api", "user", "--jq", ".login"], cwd=cwd)
        return raw.strip() or None
    except (GhError, FileNotFoundError):
        return None
