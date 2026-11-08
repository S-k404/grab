#!/bin/zsh
# Install or update everything grab needs. Safe to re-run; run it whenever a site stops working
# (YouTube etc. change often and yt-dlp / gallery-dl ship fixes within days).
set -e
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"

command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh"; exit 1; }
for pkg in ffmpeg aria2 deno; do
  brew list "$pkg" >/dev/null 2>&1 || brew install "$pkg"
done
command -v uv >/dev/null || brew install uv

[ -d .venv ] || uv venv .venv
uv pip install --python .venv/bin/python -U -e . "yt-dlp[default,curl-cffi]" gallery-dl

mkdir -p "$HOME/.local/bin"
ln -sf "$PWD/.venv/bin/grab" "$HOME/.local/bin/grab"
ln -sf "$PWD/.venv/bin/grab-gui" "$HOME/.local/bin/grab-gui"
# keep Grab.app's own engine copy in step
[ -x "$HOME/Library/Application Support/Grab/runtime/bin/python" ] && ./build_app.sh --update-runtime

echo
echo "Done. yt-dlp $(.venv/bin/yt-dlp --version), gallery-dl $(.venv/bin/gallery-dl --version)"
echo "Run 'grab --help' or 'grab-gui' (make sure ~/.local/bin is on your PATH), or double-click 'Grab GUI.command'."
