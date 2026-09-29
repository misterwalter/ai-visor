#!/usr/bin/env bash
# Open a pull request for a run's result branch, if the project's home is on
# GitHub. run_gate.sh calls this after a push; it can also be run by hand.
#
#   open_pr.sh <source clone> <workspace> <title> <body file>
#
# Prints the pull request's address. When the project's home is not on GitHub it
# says so and opens nothing, which is not an error. Uses the `gh` program, which
# must be signed in for the account that runs this.
set -u

SRC="$(realpath "${1:?source clone required}")"; WORK="$(realpath "${2:?workspace required}")"
TITLE="${3:?title required}"; BODY="$(realpath "${4:?body file required}")"

branch="$(git -C "$WORK" branch --show-current)"
case "$branch" in
  visor/?*) ;;
  *) echo "open_pr: REFUSED: only visor/ branches get a pull request, and this one is '$branch'" >&2; exit 1 ;;
esac

home="$(git -C "$SRC" remote get-url origin)" \
  || { echo "open_pr: $SRC has no origin" >&2; exit 1; }
case "$home" in
  git@github.com:*)     repo="${home#git@github.com:}" ;;
  https://github.com/*) repo="${home#https://github.com/}" ;;
  *) echo "no pull request: the project's home is not on GitHub"; exit 0 ;;
esac
repo="${repo%.git}"

command -v gh > /dev/null || { echo "open_pr: the gh program is not installed" >&2; exit 1; }
# Tasks start from main, so that is what the work is compared against. Opening a
# pull request changes nothing on main; merging it is the owner's act.
gh pr create --repo "$repo" --head "$branch" --base main --title "$TITLE" --body-file "$BODY"
