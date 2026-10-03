#!/usr/bin/env bash
# Register this repo's models, timer, dispatcher and VPN tunnel for the current user.
# Safe to run again after every `git pull`.
#
# Harness settings need no installing: the wall hands each harness the files in
# this repo (qwen-settings.json, pi/) every time it starts one.
set -eu
HERE="$(dirname "$(realpath "$0")")"

# Register a model only when its weights are already on disk; never download here.
# Registering again makes Ollama unload a loaded copy of the model, even from
# under a run, so it happens only when the recipe has changed since last time.
mkdir -p "$HOME/.local/state/visor"
# Each recipe and the name Ollama knows it by.
for pair in coder-abliterated:abliterated coder-official:official glimmer-abliterated:glimmer-abliterated; do
  name="${pair%%:*}"; file="$HERE/Modelfile.${pair#*:}"
  base="$(awk '/^FROM /{print $2}' "$file")"
  recipe="$(sha256sum < "$file")"
  registered="$HOME/.local/state/visor/registered-$name"
  if [ -f "$registered" ] && [ "$(cat "$registered")" = "$recipe" ] && ollama show "$name" > /dev/null 2>&1; then
    echo "model $name unchanged"
  elif ollama show "$base" > /dev/null 2>&1; then
    # Ollama reports progress on stderr; keep it only if the step fails.
    log="$(ollama create "$name" -f "$file" 2>&1)" \
      || { echo "$log"; echo "model $name FAILED to register"; exit 1; }
    echo "$recipe" > "$registered"
    echo "model $name registered"
  else
    echo "model $name skipped: weights for $base are not on disk"
  fi
done

mkdir -p "$HOME/.config/systemd/user" "$HOME/.local/state/visor"
cp "$HERE"/systemd/godot-update.* "$HERE/systemd/visor-vpn.service" "$HOME/.config/systemd/user/"
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

# The VPN tunnel runs only once it has its program and its settings; neither is
# downloaded or written here. Without it a note that asks for the web comes back
# unrun, and every other note is unaffected, so a tunnel that will not start is
# said and does not stop the rest.
if [ -x /srv/code/tools/wireproxy/wireproxy ] && [ -f "$HOME/.config/visor/wireproxy.conf" ]; then
  systemctl --user enable visor-vpn.service > /dev/null 2>&1
  if systemctl --user is-active --quiet visor-vpn.service || systemctl --user start visor-vpn.service; then
    echo "VPN tunnel enabled"
  else
    echo "VPN tunnel FAILED to start: systemctl --user status visor-vpn"
  fi
else
  echo "VPN tunnel NOT enabled: no wireproxy in /srv/code/tools/wireproxy, or no ~/.config/visor/wireproxy.conf (see gate/wireproxy.conf.example)"
fi
