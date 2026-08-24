"""GitHub access via the `gh` CLI.

Using `gh` rather than the REST API so authentication is whatever the user has
already set up, and so posting is a visible, auditable shell command.
"""
import json
import re
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


RE_UNKNOWN_FIELD = re.compile(r'Unknown JSON field: "([^"]+)"')


def _gh_json(argv, fields, repo=None, cwd=None):
    """Run a `gh ... --json` call, dropping fields this gh build rejects.

    Field availability varies between gh versions and between subcommands
    (`pr list` offers fewer than `pr view`). Rather than pin to one version,
    ask for what we want and retire whatever it refuses.
    """
    wanted = list(fields)
    dropped = []
    while wanted:
        try:
            return json.loads(_gh(argv + ["--json", ",".join(wanted)], repo, cwd)), dropped
        except GhError as exc:
            match = RE_UNKNOWN_FIELD.search(str(exc))
            if not match or match.group(1) not in wanted:
                raise
            wanted.remove(match.group(1))
            dropped.append(match.group(1))
    raise GhError("gh rejected every requested field")


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
    fields = ["number", "title", "body", "author", "labels", "state",
              "createdAt", "updatedAt", "url", "comments"]
    data, _ = _gh_json(["issue", "view", str(number)], fields, repo, cwd)
    return data


def fetch_pr(number, repo=None, cwd=None):
    fields = ["number", "title", "body", "author", "labels", "state",
              "createdAt", "updatedAt", "url", "comments", "files", "additions",
              "deletions", "baseRefName", "headRefName", "headRefOid", "isDraft",
              "commits"]
    data, _ = _gh_json(["pr", "view", str(number)], fields, repo, cwd)
    if not data.get("headRefOid"):
        # Older gh has no headRefOid; the last commit's oid serves the same
        # purpose, which is noticing that the branch moved.
        commits = data.get("commits") or []
        if commits:
            last = commits[-1]
            data["headRefOid"] = last.get("oid") or last.get("sha")
    return data


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
    argv = ["issue", "list", "--state", "open", "--limit", str(limit)]
    if label:
        argv += ["--label", label]
    fields = ["number", "title", "labels", "updatedAt", "author"]
    rows, _ = _gh_json(argv, fields, repo, cwd)
    for it in rows:
        it["kind"] = "issue"
        yield it


def list_open_prs(label=None, repo=None, cwd=None, limit=20):
    """Open PRs. headRefOid is requested but not required — older gh builds do
    not offer it on `pr list`, in which case updatedAt alone drives the loop."""
    argv = ["pr", "list", "--state", "open", "--limit", str(limit)]
    if label:
        argv += ["--label", label]
    fields = ["number", "title", "labels", "updatedAt", "author",
              "headRefOid", "isDraft"]
    rows, _ = _gh_json(argv, fields, repo, cwd)
    for it in rows:
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
