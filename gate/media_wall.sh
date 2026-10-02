#!/usr/bin/env bash
# Run one media tool (whisper.cpp, Kokoro, ComfyUI, ffmpeg) inside a wall: a reduced
# view of this machine, enforced by the kernel through bubblewrap.
#
#   media_wall.sh <job dir> [--ro PATH]... -- <command> [args...]
#
# <job dir>   the job's scratch folder: its input copy, its output, and for ComfyUI
#             the socket visor talks to it through. The only place it can write.
# --ro PATH   a path it may read: its program, its Python environment, its models.
#
# There is no network: a tool, or an add-on someone slipped into one, cannot fetch
# or send anything. Nothing of the account's exists inside: no home, no keys, no
# notes, no other jobs. Only the job folder is writable.
set -u

JOB="$(realpath "${1:?job dir required}")"; shift
readable=()
while [ $# -gt 0 ] && [ "$1" != "--" ]; do
  case "$1" in
    --ro) path="$(realpath "${2:?--ro needs a path}")"; readable+=(--ro-bind "$path" "$path"); shift 2 ;;
    *) echo "media_wall: unknown option $1" >&2; exit 2 ;;
  esac
done
[ "${1:-}" = "--" ] && shift
[ $# -gt 0 ] || { echo "media_wall: command required" >&2; exit 2; }

# The account list, cut down to this account. The real one names everybody.
accounts="$(mktemp -d)"
trap 'rm -rf "$accounts"' EXIT
getent passwd root nobody "$(id -u)" > "$accounts/passwd"
getent group root nogroup "$(id -g)" > "$accounts/group"

# The model server keeps its home, with its keys, under /usr on a default install.
hidden=()
[ -d /usr/share/ollama ] && hidden+=(--tmpfs /usr/share/ollama)

bwrap \
  --unshare-all --die-with-parent --new-session --clearenv \
  --setenv HOME /home/media --setenv PATH /usr/local/bin:/usr/bin:/bin \
  --setenv LANG C.UTF-8 --setenv TERM dumb \
  --ro-bind /usr /usr \
  --symlink usr/bin /bin --symlink usr/sbin /sbin \
  --symlink usr/lib /lib --symlink usr/lib64 /lib64 \
  --ro-bind /etc /etc \
  --ro-bind "$accounts/passwd" /etc/passwd --ro-bind "$accounts/group" /etc/group \
  "${hidden[@]}" \
  --proc /proc --dev /dev --tmpfs /tmp --tmpfs /run --tmpfs /home --dir /home/media \
  "${readable[@]}" \
  --bind "$JOB" "$JOB" \
  --chdir "$JOB" \
  "$@"
