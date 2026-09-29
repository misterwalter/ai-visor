#!/usr/bin/env bash
# Run a command inside the wall: a reduced view of this machine, enforced by the
# kernel through bubblewrap, in which only the workspace can be written, there
# is no network, and nothing else of the account's exists.
#
#   wall.sh <workspace> <wall dir> <log dir> <rw|ro> <command> [args...]
#
# <workspace>  the project clone the agent works in
# <wall dir>   holds the two door sockets; seen inside as /run/gate
# <log dir>    where the harness keeps its own records; writable inside
# rw | ro      whether the workspace can be written (ro for a plan-only round)
#
# What is visible inside, and how:
#   read-only   /usr and /etc, the harnesses, their settings from this repo, gate/inside
#   writable    the workspace (its .git read-only), the log dir, an empty home, /tmp
#   absent      everything else: the real home, other runs, the source clone, notes
# The network is a private one with nothing on it. The command is started through
# inside/start-agent, which connects the model's usual address to the model door.
set -u

WORK="$(realpath "${1:?workspace required}")"; WALL="$(realpath "${2:?wall dir required}")"
LOGS="$(realpath "${3:?log dir required}")"; ACCESS="${4:?rw or ro required}"
shift 4
[ $# -gt 0 ] || { echo "wall: command required" >&2; exit 2; }

HERE="$(dirname "$(realpath "$0")")"
HARNESSES="$HOME/.npm-global"
for needed in "$WORK/.git" "$WALL" "$LOGS" "$HARNESSES"; do
  [ -e "$needed" ] || { echo "wall: missing $needed" >&2; exit 1; }
done

case "$ACCESS" in
  # git runs hook scripts and the commands named in its config, and the runner
  # uses git on this workspace afterwards, outside the wall. A writable .git
  # would be a way out.
  rw) workspace=(--bind "$WORK" "$WORK" --ro-bind "$WORK/.git" "$WORK/.git") ;;
  ro) workspace=(--ro-bind "$WORK" "$WORK") ;;
  *)  echo "wall: access must be rw or ro, got $ACCESS" >&2; exit 2 ;;
esac

# The account list, cut down to this account. The real one names everybody.
getent passwd root nobody "$(id -u)" > "$WALL/passwd"
getent group root nogroup "$(id -g)" > "$WALL/group"

# The model server keeps its home, with its keys, under /usr on a default install.
hidden=()
[ -d /usr/share/ollama ] && hidden+=(--tmpfs /usr/share/ollama)

# Each harness reads its settings from the home folder. Inside the wall those
# are this repo's files, so a run cannot pick up a copy that has drifted.
settings=(
  --dir "$HOME/.qwen"
  --ro-bind "$HERE/qwen-settings.json" "$HOME/.qwen/settings.json"
  --dir "$HOME/.pi/agent"
  --ro-bind "$HERE/pi/settings.json" "$HOME/.pi/agent/settings.json"
  --ro-bind "$HERE/pi/models.json" "$HOME/.pi/agent/models.json"
)

# Nothing is inherited from the caller's environment except the harnesses' own
# QWEN_* and PI_* settings.
environment=(
  --setenv HOME "$HOME"
  --setenv USER "$(id -un)"
  --setenv PATH "$HERE/inside:$HARNESSES/bin:/usr/local/bin:/usr/bin:/bin"
  --setenv LANG C.UTF-8
  --setenv TERM dumb
)
for name in $(compgen -v QWEN_) $(compgen -v PI_); do
  environment+=(--setenv "$name" "${!name}")
done

exec bwrap \
  --unshare-all --die-with-parent --new-session --clearenv \
  "${environment[@]}" \
  --ro-bind /usr /usr \
  --symlink usr/bin /bin --symlink usr/sbin /sbin \
  --symlink usr/lib /lib --symlink usr/lib64 /lib64 \
  --ro-bind /etc /etc \
  --ro-bind "$WALL/passwd" /etc/passwd --ro-bind "$WALL/group" /etc/group \
  "${hidden[@]}" \
  --proc /proc --dev /dev --tmpfs /tmp --tmpfs /run \
  --tmpfs "$HOME" \
  --ro-bind "$HARNESSES" "$HARNESSES" \
  "${settings[@]}" \
  --ro-bind "$HERE/inside" "$HERE/inside" \
  --ro-bind "$WALL" /run/gate \
  "${workspace[@]}" \
  --bind "$LOGS" "$LOGS" \
  --chdir "$WORK" \
  "$HERE/inside/start-agent" "$@"
