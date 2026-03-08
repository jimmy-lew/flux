#!/usr/bin/env bash
# ============================================================
#  install.sh — sets up the full mpv + flux environment
#  - Installs flux.sh as a user command
#  - Copies mpv.conf and modernz.conf to correct locations
#  - Installs po5/mpv_sponsorblock into mpv scripts
# ============================================================

set -euo pipefail

RED=$'\033[1;31m'; GRN=$'\033[1;32m'; YLW=$'\033[1;33m'
CYN=$'\033[1;36m'; BLD=$'\033[1m'; RST=$'\033[0m'

log()  { echo -e "${GRN}[✔]${RST} $*"; }
warn() { echo -e "${YLW}[!]${RST} $*"; }
err()  { echo -e "${RED}[✘]${RST} $*" >&2; }
hdr()  { echo -e "\n${CYN}${BLD}━━  $*  ━━${RST}"; }

DIR="$(cd "$(dirname "$0")" && pwd)"
CMD_NAME="${1:-flux}"
INSTALL_DIR="$HOME/.local/bin"
SCRIPT_SRC="$DIR/flux"
INSTALL_DEST="$INSTALL_DIR/$CMD_NAME"
MPV_DIR="$HOME/.config/mpv"

echo -e "\n${CYN}${BLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RST}"
echo -e "${CYN}${BLD}  flux + mpv installer${RST}"
echo -e "${CYN}${BLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RST}\n"

# ── 1. Install the flux command ──────────────────────────────
hdr "Installing '$CMD_NAME' command"

SYSTEM_CMD="$(command -v "$CMD_NAME" 2>/dev/null || true)"
if [[ -n "$SYSTEM_CMD" && "$SYSTEM_CMD" != "$INSTALL_DEST" ]]; then
  warn "'$CMD_NAME' already exists at $SYSTEM_CMD"
  warn "Your user command will shadow it. Use a different name with: ./install.sh myflux"
  echo
  read -rp "Continue installing as '$CMD_NAME'? [y/N] " confirm
  [[ "${confirm,,}" == "y" ]] || { echo "Aborted."; exit 0; }
fi

if [[ ! -f "$SCRIPT_SRC" ]]; then
  err "Cannot find flux next to this installer (expected: $SCRIPT_SRC)"
  exit 1
fi

mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_SRC" "$INSTALL_DEST"
chmod +x "$INSTALL_DEST"
log "Installed: $INSTALL_DEST"

# ── PATH ──────────────────────────────────────────────────────
SHELL_RC=""
case "${SHELL:-}" in
  */zsh)  SHELL_RC="$HOME/.zshrc" ;;
  */bash) SHELL_RC="$HOME/.bashrc" ;;
  *)      SHELL_RC="$HOME/.profile" ;;
esac

if echo "$PATH" | tr ':' '\n' | grep -qxF "$INSTALL_DIR"; then
  log "$INSTALL_DIR is already in PATH"
else
  warn "$INSTALL_DIR not in PATH — adding to $SHELL_RC"
  { echo ""; echo "# Added by flux installer"; echo 'export PATH="$HOME/.local/bin:$PATH"'; } >> "$SHELL_RC"
  log "Added PATH entry to $SHELL_RC"
  warn "Run: source $SHELL_RC  (or open a new terminal)"
fi

# ── 2. mpv config files ───────────────────────────────────────
hdr "Installing mpv config"

mkdir -p "$MPV_DIR/scripts" "$MPV_DIR/script-opts"

if [[ -f "$DIR/mpv.conf" ]]; then
  [[ -f "$MPV_DIR/mpv.conf" ]] && cp "$MPV_DIR/mpv.conf" "$MPV_DIR/mpv.conf.bak" && warn "Backed up existing mpv.conf → mpv.conf.bak"
  cp "$DIR/mpv.conf" "$MPV_DIR/mpv.conf"
  log "Installed mpv.conf → $MPV_DIR/mpv.conf"
else
  warn "mpv.conf not found next to installer — skipping"
fi

if [[ -f "$DIR/modernz.conf" ]]; then
  cp "$DIR/modernz.conf" "$MPV_DIR/script-opts/modernz.conf"
  log "Installed modernz.conf → $MPV_DIR/script-opts/modernz.conf"
else
  warn "modernz.conf not found next to installer — skipping"
fi

# ── 3. SponsorBlock ───────────────────────────────────────────
hdr "Installing SponsorBlock for mpv"

for dep in git python3; do
  if ! command -v "$dep" &>/dev/null; then
    err "$dep is required for SponsorBlock: sudo pacman -S $dep"
    exit 1
  fi
done

SB_SCRIPTS_DIR="$MPV_DIR/scripts"
SB_TMP=$(mktemp -d)
trap 'rm -rf "$SB_TMP"' EXIT

# ── Install skip_intro.lua ─────────────────────────────────────────────────
hdr "Installing skip_intro.lua"
SKIP_INTRO_SRC="$DIR/skip_intro.lua"
if [[ ! -f "$SKIP_INTRO_SRC" ]]; then
  err "skip_intro.lua not found next to install.sh (expected: $SKIP_INTRO_SRC)"
  exit 1
fi
cp "$SKIP_INTRO_SRC" "$MPV_DIR/scripts/skip_intro.lua"
log "Installed skip_intro.lua → $MPV_DIR/scripts/skip_intro.lua"

if [[ -f "$SB_SCRIPTS_DIR/sponsorblock.lua" ]]; then
  warn "SponsorBlock already installed — updating..."
  rm -rf "$SB_SCRIPTS_DIR/sponsorblock_shared"
fi

log "Cloning po5/mpv_sponsorblock..."
git clone --quiet --depth=1 "https://github.com/po5/mpv_sponsorblock.git" "$SB_TMP/repo"
cp "$SB_TMP/repo/sponsorblock.lua" "$SB_SCRIPTS_DIR/sponsorblock.lua"
cp -r "$SB_TMP/repo/sponsorblock_shared" "$SB_SCRIPTS_DIR/sponsorblock_shared"
log "Installed sponsorblock.lua + sponsorblock_shared/"

# Patch in /live/ and /shorts/ URL patterns (upstream only handles /watch?v= and youtu.be)
sed -i \
  '/\/watch\.\*\[?&\]v=/a\        "youtube%.com\/live\/([%w-_]+).*",\n        "youtube%.com\/shorts\/([%w-_]+).*",' \
  "$SB_SCRIPTS_DIR/sponsorblock.lua"
log "Patched sponsorblock.lua with /live/ and /shorts/ URL patterns"

cat > "$MPV_DIR/script-opts/sponsorblock.conf" << 'SBCONF'
# SponsorBlock configuration
# https://github.com/po5/mpv_sponsorblock

server_address=https://sponsor.ajay.app

# Python 3 executable path — must be python3 on Arch Linux
python_path=python3

# Categories to fetch (shown as chapter nibbles on the seekbar for non-skipped ones)
# Options: sponsor,intro,outro,interaction,selfpromo,preview,music_offtopic,filler
categories=sponsor,intro,outro,interaction,selfpromo,filler

# Categories to auto-skip (must be a subset of categories above)
skip_categories=sponsor,intro,outro

# Show OSD message when a segment is skipped

# Skip each segment only once per session
skip_once=yes

# Use local SQLite database (downloaded on first run, faster than live API)
local_database=yes

# Auto-update the database on first run
auto_update=yes

# How long between database updates (format: Xd, Xh, Xm)
auto_update_interval=6h
SBCONF

log "Installed script-opts/sponsorblock.conf"

# ── 4. Summary ────────────────────────────────────────────────
echo -e "\n${GRN}${BLD}All done!${RST}\n"
echo -e "  ${BLD}Run videos:${RST}"
echo -e "    ${CMD_NAME} 'https://youtu.be/dQw4w9WgXcQ'"
echo -e "    ${CMD_NAME} -s anime4k 'https://hianime.to/watch/...'"
echo -e ""
echo -e "  ${BLD}SponsorBlock keybinds (in mpv):${RST}"
echo -e "    g     — set segment start/end boundary"
echo -e "    G     — submit segment to SponsorBlock"
echo -e "    h / H — upvote / downvote last skipped segment"
echo -e ""
echo -e "  ${BLD}Skip intro (-i flag):${RST}"
echo -e "    flux -n -i 'https://hianime.to/watch/show?ep=1234'"
echo -e "    I     — toggle skip intro on/off while playing"
echo -e ""
echo -e "  ${BLD}Config locations:${RST}"
echo -e "    $MPV_DIR/mpv.conf"
echo -e "    $MPV_DIR/script-opts/modernz.conf"
echo -e "    $MPV_DIR/script-opts/sponsorblock.conf"
echo -e "    $MPV_DIR/scripts/sponsorblock.lua"
echo -e "    $MPV_DIR/scripts/skip_intro.lua"
echo -e ""

if ! echo "$PATH" | tr ':' '\n' | grep -qxF "$INSTALL_DIR"; then
  echo -e "${YLW}Remember to reload your shell:${RST} source $SHELL_RC\n"
fi
