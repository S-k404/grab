#!/bin/zsh
# Double-click in Finder to open the grab GUI in your browser.
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"
[ -x .venv/bin/grab-gui ] || ./setup.sh
exec .venv/bin/grab-gui "$@"
