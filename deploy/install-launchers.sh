#!/bin/sh
# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
# Put the launcher in the menu and on the desktop, and the autostart entry in place.
# For the user who runs it. No sudo. Safe to run again.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)

chmod +x "$HERE/scaleconnect-web.sh" "$HERE/scaleconnect-mcp.sh" "$HERE/install-launchers.sh" 2>/dev/null || true

mkdir -p "$HOME/.local/share/applications" "$HOME/.config/autostart"
cp "$HERE/scaleconnect.desktop" "$HOME/.local/share/applications/scaleconnect.desktop"
cp "$HERE/scaleconnect-autostart.desktop" "$HOME/.config/autostart/scaleconnect-autostart.desktop"
cp "$HERE/scaleconnect-mcp-autostart.desktop" "$HOME/.config/autostart/scaleconnect-mcp-autostart.desktop"
echo "Menu entry and autostart entries installed (page and MCP server)."

DESK="$HOME/Desktop"
if [ -d "$DESK" ]; then
    cp "$HERE/scaleconnect.desktop" "$DESK/scaleconnect.desktop"
    chmod +x "$DESK/scaleconnect.desktop"
    # Some desktops need the launcher marked as trusted.
    if command -v gio >/dev/null 2>&1; then
        gio set "$DESK/scaleconnect.desktop" metadata::trusted true >/dev/null 2>&1 || true
    fi
    echo "Desktop icon installed."
else
    echo "There is no $DESK folder, so no desktop icon was made."
fi

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
fi
echo "Done. The launcher expects the repo at $HOME/CCScaleApp/ScaleConnect."
