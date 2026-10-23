#!/bin/sh
# Run the bridge in the background at login: launchd on macOS, a systemd user unit on Linux.
# Usage: install-service.sh [bridge options...]   e.g. install-service.sh --token s3cret
#        install-service.sh --uninstall
# Re-run to change the options.
set -eu

NAME="agent-sessions-bridge"
BRIDGE="$(cd "$(dirname "$0")" && pwd)/agent_sessions_bridge.py"
PYTHON="$(command -v python3)"

xml_escape() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

install_launchd() {
  plist="$HOME/Library/LaunchAgents/$NAME.plist"
  launchctl bootout "gui/$(id -u)/$NAME" 2>/dev/null || true
  if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$plist"
    echo "Removed $NAME"
    return
  fi
  args="    <string>$(xml_escape "$PYTHON")</string>
    <string>$(xml_escape "$BRIDGE")</string>"
  for arg in "$@"; do
    args="$args
    <string>$(xml_escape "$arg")</string>"
  done
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$NAME</string>
  <key>ProgramArguments</key>
  <array>
$args
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/$NAME.log</string>
</dict>
</plist>
EOF
  launchctl bootstrap "gui/$(id -u)" "$plist"
  echo "Installed $NAME; logs in ~/Library/Logs/$NAME.log"
}

install_systemd() {
  unit="$HOME/.config/systemd/user/$NAME.service"
  systemctl --user disable --now "$NAME" 2>/dev/null || true
  if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$unit"
    systemctl --user daemon-reload
    echo "Removed $NAME"
    return
  fi
  exec_start="\"$PYTHON\" \"$BRIDGE\""
  for arg in "$@"; do
    exec_start="$exec_start \"$arg\""
  done
  mkdir -p "$(dirname "$unit")"
  cat > "$unit" <<EOF
[Unit]
Description=Agent Sessions bridge for the Vobot Mini Dock
After=network-online.target

[Service]
ExecStart=$exec_start
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now "$NAME"
  echo "Installed $NAME; logs: journalctl --user -u $NAME"
}

case "$(uname -s)" in
  Darwin) install_launchd "$@" ;;
  Linux) install_systemd "$@" ;;
  *) echo "Unsupported OS; run $BRIDGE directly." >&2; exit 1 ;;
esac
