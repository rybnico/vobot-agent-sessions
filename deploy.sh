#!/bin/sh
# Package agent_sessions/ and install it on a Mini Dock through its web app manager.
# Usage: ./deploy.sh <device-address>   (or set VOBOT_HOST)
# The device accepts uploads only while Settings > Application Settings is open on it.
set -eu

DEVICE="${1:-${VOBOT_HOST:-}}"
if [ -z "$DEVICE" ]; then
  echo "Usage: $0 <device-address>   (or set VOBOT_HOST)" >&2
  exit 2
fi

cd "$(dirname "$0")"
APP_DIR="agent_sessions"
NAME="$(sed -n 's/^  name: *//p' "$APP_DIR/manifest.yml")"
VERSION="$(sed -n 's/^  version: *//p' "$APP_DIR/manifest.yml")"
mkdir -p dist
ZIP="dist/agent_sessions-$VERSION.zip"
rm -f "$ZIP"
zip -qr "$ZIP" "$APP_DIR" -x '*/.DS_Store' '*/__pycache__/*'

if curl -s -m 5 "http://$DEVICE/apps" | grep -q "Settings Not Available"; then
  echo "The device only accepts uploads while Settings > Application Settings is open on it." >&2
  exit 1
fi

echo "Uploading $ZIP to $DEVICE ..."
curl -sS --fail-with-body -m 60 \
  -F "file=@$ZIP;filename=$NAME;type=application/zip" \
  -F "version=$VERSION" \
  -F "id=local-agent-sessions" \
  "http://$DEVICE/api/v1/app/upload"
echo
