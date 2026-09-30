#!/usr/bin/env bash
# Register this repo's models, timer and dispatcher for the current user.
# Safe to run again after every `git pull`.
#
# Harness settings need no installing: the wall hands each harness the files in
# this repo (qwen-settings.json, pi/) every time it starts one.
set -eu
HERE="$(dirname "$(realpath "$0")")"

# Register a model only when its weights are already on disk; never download here.
for build in abliterated official; do
  base="$(awk '/^FROM /{print $2}' "$HERE/Modelfile.$build")"
  if ollama show "$base" > /dev/null 2>&1; then
    # Ollama reports progress on stderr; keep it only if the step fails.
    log="$(ollama create "coder-$build" -f "$HERE/Modelfile.$build" 2>&1)" \
      || { echo "$log"; echo "model coder-$build FAILED to register"; exit 1; }
    echo "model coder-$build registered"
  else
    echo "model coder-$build skipped: weights for $base are not on disk"
  fi
done

mkdir -p "$HOME/.config/systemd/user" "$HOME/.local/state/visor"
cp "$HERE"/systemd/godot-update.* "$HOME/.config/systemd/user/"
sed "s|@GATE@|$HERE|" "$HERE/systemd/visor-dispatch.service" > "$HOME/.config/systemd/user/visor-dispatch.service"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
systemctl --user daemon-reload
systemctl --user enable --now godot-update.timer > /dev/null 2>&1
echo "godot update timer enabled"

# The dispatcher runs only once it has its settings; without them it would fail
# on every start. It is started here if it is not running. After a self-update
# it restarts itself, so new code takes effect between runs, never during one.
if [ -f "$HOME/.config/visor/visor.conf" ]; then
  systemctl --user enable visor-dispatch.service > /dev/null 2>&1
  systemctl --user is-active --quiet visor-dispatch.service || systemctl --user start visor-dispatch.service
  echo "dispatcher enabled"
else
  echo "dispatcher NOT enabled: no ~/.config/visor/visor.conf (see gate/visor.conf.example)"
fi
