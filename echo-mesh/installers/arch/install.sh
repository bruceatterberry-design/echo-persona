#!/bin/sh
# Echo Mesh agent for Arch Linux / Omarchy. One command, run from a checkout of this repo:
#
#   sh installers/arch/install.sh                      join automatically (same home network); approve on the Mac
#   sh installers/arch/install.sh --host ADDRESS --code 1234     join with a pairing code (outside the home network)
#   sh installers/arch/install.sh --role peer          skip the question: "worker" (default) or "peer" (this computer has its own AI)
#   sh installers/arch/install.sh --accept-terms       the terms were already read; do not show them again
#   sh installers/arch/install.sh --autostart no       do not start at boot (default asks; "yes" = start at every boot)
#   sh installers/arch/install.sh --uninstall          remove it completely
#
# It installs only the small agent (no root needed except for installing python if it is missing), keeps it under
# ~/.local/share/echo-mesh, and runs it as a systemd USER service that starts at boot.
set -eu

SRC="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="${ECHOMESH_HOME:-$HOME/.local/share/echo-mesh}"
UNITDIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNITDIR/echomesh-agent.service"
IDENT="${ECHOMESH_IDENT:-$HOME/.echo-mesh-agent}"
FILES="__init__.py common.py inventory.py governor.py policy.py brainlet.py cognition.py speedup.py brainpkg.py ed25519.py boardbot.py agent.py agreement.py cli_setup.py winapp_core.py"

say() { printf '%s\n' "$*"; }

if [ "${1:-}" = "--uninstall" ]; then
  systemctl --user disable --now echomesh-agent 2>/dev/null || true
  rm -f "$UNIT"; rm -rf "$DEST" "$IDENT"
  systemctl --user daemon-reload 2>/dev/null || true
  say "Echo Mesh removed. (The Mesh owner can also remove this computer from the plugin.)"
  exit 0
fi

# 1. python (the only dependency)
if ! command -v python3 >/dev/null 2>&1; then
  say "Installing python (needs your password once)..."
  sudo pacman -S --needed --noconfirm python
fi

# 2. the agent files
mkdir -p "$DEST/mesh"
for f in $FILES; do cp "$SRC/mesh/$f" "$DEST/mesh/$f"; done
say "Installed the agent to $DEST"

# 3. splash, terms and disclaimer, Mesh configuration (same three steps as the Windows installer; asked once, kept in policy.json)
cd "$DEST"
SETUP=""
case " $* " in *" --accept-terms "*) SETUP="$SETUP --accept-terms" ;; esac
ROLE="$(printf '%s ' "$@" | sed -n 's/.*--role \([a-z]*\).*/\1/p')"
[ -n "$ROLE" ] && SETUP="$SETUP --role $ROLE"
AUTO="$(printf '%s ' "$@" | sed -n 's/.*--autostart \([a-z]*\).*/\1/p')"
[ -n "$AUTO" ] && SETUP="$SETUP --autostart $AUTO"
if ! grep -q terms_accepted "$IDENT/policy.json" 2>/dev/null || [ -n "$ROLE" ] || [ -n "$AUTO" ] || ! grep -q autostart "$IDENT/policy.json" 2>/dev/null; then
  python3 -m mesh.cli_setup --dir "$IDENT" $SETUP || { say "Setup declined; nothing was installed."; rm -rf "$DEST"; exit 1; }
fi
# the join command only understands its own flags
ARGS=""; skip=0
for a in "$@"; do
  if [ "$skip" = 1 ]; then skip=0; continue; fi
  case "$a" in --role|--autostart) skip=1 ;; --accept-terms) ;; *) ARGS="$ARGS $a" ;; esac
done

# 4. join (only if this computer has not joined yet)
if [ ! -f "$IDENT/identity.json" ]; then
  case " $* " in
    *" --code "*) python3 -m mesh.agent enroll --dir "$IDENT" $ARGS ;;     # pairing code (outside the home network)
    *)            python3 -m mesh.agent join --dir "$IDENT" $ARGS ;;       # automatic: find the Mesh, show a number, wait for a yes
  esac
else
  say "Already joined; keeping the existing identity."
fi

# 5. run it in the background, now and at every boot
mkdir -p "$UNITDIR"
cat > "$UNIT" <<EOF
[Unit]
Description=Echo Mesh agent
After=network-online.target

[Service]
WorkingDirectory=$DEST
ExecStart=$(command -v python3) -m mesh.agent run --dir $IDENT
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
if grep -q '"autostart": false' "$IDENT/policy.json" 2>/dev/null; then
  systemctl --user disable echomesh-agent 2>/dev/null || true       # asked: do not start at boot
  systemctl --user start echomesh-agent                              # but run it now; later: systemctl --user start echomesh-agent
  say "Not set to start at boot. Start it any time with:  systemctl --user start echomesh-agent"
else
  systemctl --user enable --now echomesh-agent
  loginctl enable-linger "$USER" 2>/dev/null || true
fi
sleep 2
say "Echo Mesh agent: $(systemctl --user is-active echomesh-agent)"
