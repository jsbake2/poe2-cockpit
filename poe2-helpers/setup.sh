#!/usr/bin/env bash
# poe2-helpers one-shot setup.
# - Installs system packages (Python, tk, xdotool)
# - Creates .venv and installs Python deps
# - Adds current user to the `input` group (required for global hotkeys)
#
# After this runs, log out and log back in so the input-group change takes effect,
# then run ./run.sh.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

log() { printf "\n\033[1;36m==> %s\033[0m\n" "$*"; }
warn() { printf "\n\033[1;33m!! %s\033[0m\n" "$*"; }
die()  { printf "\n\033[1;31mXX %s\033[0m\n" "$*" >&2; exit 1; }

need_sudo=0
if [ "$(id -u)" -ne 0 ]; then need_sudo=1; fi
SUDO() { if [ "$need_sudo" -eq 1 ]; then sudo "$@"; else "$@"; fi; }

# --- 1. system packages ----------------------------------------------------
if command -v pacman >/dev/null 2>&1; then
  log "Installing system packages via pacman (python, tk, xdotool)"
  SUDO pacman -S --needed --noconfirm python tk xdotool
elif command -v apt-get >/dev/null 2>&1; then
  log "Installing system packages via apt (python3-venv, python3-tk, xdotool)"
  SUDO apt-get update
  SUDO apt-get install -y python3 python3-venv python3-tk xdotool
elif command -v dnf >/dev/null 2>&1; then
  log "Installing system packages via dnf (python3-tkinter, xdotool)"
  SUDO dnf install -y python3 python3-tkinter xdotool
else
  warn "Unknown package manager. Please install manually: python3 (>=3.11), tkinter, xdotool"
fi

# --- 2. sanity-check tkinter -----------------------------------------------
if ! python3 -c "import tkinter" >/dev/null 2>&1; then
  die "tkinter is not importable from python3 — install your distro's python tk package"
fi
if ! command -v xdotool >/dev/null 2>&1; then
  die "xdotool not found — focus detection requires it"
fi

# --- 3. venv + python deps -------------------------------------------------
if [ ! -d .venv ]; then
  log "Creating .venv"
  python3 -m venv .venv
fi
log "Installing Python deps (evdev, pyyaml)"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

# --- 4. input-group membership --------------------------------------------
if id -nG "$USER" | tr ' ' '\n' | grep -qx input; then
  log "User '$USER' already in 'input' group"
else
  log "Adding user '$USER' to 'input' group (required for /dev/input/event* access)"
  SUDO gpasswd -a "$USER" input
  warn "You were added to the 'input' group."
  warn "LOG OUT and LOG BACK IN before running ./run.sh, or the service won't see your keyboards."
fi

log "Setup complete. Next:"
echo "    1. (if you just got added to the 'input' group) log out + back in"
echo "    2. ./run.sh"
echo "    3. edit hotkeys.yaml to taste and restart"
