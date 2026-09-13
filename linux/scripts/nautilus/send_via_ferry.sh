#!/usr/bin/env bash
# Nautilus script: Send selected file(s) via Ferry.
# Install: copy to ~/.local/share/nautilus/scripts/
#          rename to "Send via Ferry" and chmod +x
#
# Usage: Right-click file(s) in Nautilus → Scripts → "Send via Ferry"
set -euo pipefail

if [ -z "${NAUTILUS_SCRIPT_SELECTED_FILE_PATHS:-}" ]; then
    notify-send "Ferry" "No files selected" --icon=dialog-warning 2>/dev/null || true
    exit 1
fi

SEND_ARGS=()
while IFS= read -r filepath; do
    [ -n "$filepath" ] && SEND_ARGS+=("--send" "$filepath")
done <<< "$NAUTILUS_SCRIPT_SELECTED_FILE_PATHS"

if [ ${#SEND_ARGS[@]} -eq 0 ]; then
    notify-send "Ferry" "No valid file paths found" --icon=dialog-warning 2>/dev/null || true
    exit 1
fi

python3 -m ferry_linux "${SEND_ARGS[@]}" &
