#!/usr/bin/env bash
# What huashu-motion (code-made explainer / launch videos) needs on an AI Workstation computer, Proxmox VM or
# InstaBox cloud machine: Node 22+, Chrome/Chromium, FFmpeg, and for a Chinese voiceover Kokoro (uv + Python 3.12)
# and whisper-cli. task-247 never got that far, but the README lists these and was only tested on macOS
# (no system voice on Linux).
#
#   bash ~/ai-workstation/deploy/vm/motion-setup.sh          install what is missing (idempotent)
#   bash ~/ai-workstation/deploy/vm/motion-setup.sh check    show what is there
set -u
SUDO=""; [ "$(id -u)" != 0 ] && { sudo -n true 2>/dev/null && SUDO="sudo -n" || SUDO="sudo"; }
SK=""; for d in ~/.claude/skills/huashu-motion ~/.codex/skills/huashu-motion ~/.agents/skills/huashu-motion; do [ -d "$d" ] && SK=$d && break; done
nodev() { node -v 2>/dev/null | sed 's/^v//; s/\..*//'; }
check() {
  [ "$(nodev || echo 0)" -ge 22 ] 2>/dev/null && echo "ok      node $(node -v)" || echo "missing node 22+ (have: $(node -v 2>/dev/null || echo none))"
  b=$(command -v google-chrome || command -v chromium || command -v chromium-browser); [ -n "$b" ] && echo "ok      browser $b" || echo "missing chrome/chromium"
  command -v ffmpeg >/dev/null && echo "ok      ffmpeg" || echo "missing ffmpeg"
  command -v uv >/dev/null && echo "ok      uv (Kokoro voice)" || echo "missing uv (needed for Kokoro Chinese voice)"
  command -v whisper-cli >/dev/null && echo "ok      whisper-cli" || echo "optional whisper-cli (word-level alignment)"
  [ -n "$SK" ] && echo "ok      skill $SK" || echo "missing huashu-motion skill (npx skills add alchaincyf/huashu-motion)"
  [ -n "$SK" ] && [ -x "$SK/bin/om" ] && "$SK/bin/om" doctor 2>&1 | tail -20
}
[ "${1:-}" = check ] && { check; exit 0; }
export DEBIAN_FRONTEND=noninteractive
if [ "$(nodev || echo 0)" -lt 22 ] 2>/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | $SUDO -E bash - && $SUDO apt-get install -y -qq nodejs
fi
command -v ffmpeg >/dev/null || $SUDO apt-get install -y -qq ffmpeg
if ! command -v google-chrome >/dev/null && ! command -v chromium >/dev/null && ! command -v chromium-browser >/dev/null; then
  curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && $SUDO apt-get install -y -qq /tmp/chrome.deb || $SUDO apt-get install -y -qq chromium
fi
command -v uv >/dev/null || { curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"; }
command -v uv >/dev/null && uv python install 3.12 >/dev/null 2>&1 || true
[ -z "$SK" ] && command -v npx >/dev/null && npx -y skills add alchaincyf/huashu-motion -a claude-code -y || true
SK=""; for d in ~/.claude/skills/huashu-motion ~/.codex/skills/huashu-motion ~/.agents/skills/huashu-motion; do [ -d "$d" ] && SK=$d && break; done
[ -n "$SK" ] && [ -x "$SK/bin/om" ] && { "$SK/bin/om" samples build >/dev/null 2>&1 || true; }
check
