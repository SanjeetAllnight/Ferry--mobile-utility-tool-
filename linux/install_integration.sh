#!/usr/bin/env bash
# Ferry System Integration Installer
# Installs the systemd user service, desktop entry, and optional autostart.
#
# Usage:
#   ./install_integration.sh [--uninstall]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"

install_integration() {
    echo "Installing Ferry system integration..."

    # 1. systemd user service
    mkdir -p "$SYSTEMD_USER_DIR"
    cp "$SCRIPT_DIR/systemd/ferry.service" "$SYSTEMD_USER_DIR/ferry.service"
    systemctl --user daemon-reload
    echo "  ✓ Installed systemd user service: $SYSTEMD_USER_DIR/ferry.service"

    # Enable autostart via systemd (optional — prompt)
    read -r -p "  Enable Ferry daemon to start automatically on login? [y/N] " response
    if [[ "${response,,}" == "y" ]]; then
        systemctl --user enable ferry.service
        echo "  ✓ Ferry daemon enabled for autostart"
    fi

    # 2. .desktop entry
    mkdir -p "$DESKTOP_DIR"
    cp "$SCRIPT_DIR/desktop/dev.ferry.Ferry.desktop" "$DESKTOP_DIR/dev.ferry.Ferry.desktop"
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
    echo "  ✓ Installed .desktop entry: $DESKTOP_DIR/dev.ferry.Ferry.desktop"

    echo ""
    echo "Ferry integration installed successfully."
    echo ""
    echo "To start the daemon now: systemctl --user start ferry"
    echo "To open the Ferry UI:    ferry"
}

uninstall_integration() {
    echo "Uninstalling Ferry system integration..."

    systemctl --user disable --now ferry.service 2>/dev/null || true
    rm -f "$SYSTEMD_USER_DIR/ferry.service"
    systemctl --user daemon-reload
    echo "  ✓ Removed systemd user service"

    rm -f "$DESKTOP_DIR/dev.ferry.Ferry.desktop"
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
    echo "  ✓ Removed .desktop entry"

    rm -f "$AUTOSTART_DIR/dev.ferry.Ferry.desktop"
    echo "  ✓ Removed autostart entry (if any)"

    echo ""
    echo "Ferry integration uninstalled."
}

case "${1:-}" in
    --uninstall) uninstall_integration ;;
    *) install_integration ;;
esac
