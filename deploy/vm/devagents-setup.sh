#!/usr/bin/env bash
# AIW computer (VM 104 / InstaBox cloud box): WorkBuddy desktop + CodeBuddy CLI (WorkBuddy 终端) + REA reverse-engineering MCP.
#   bash devagents-setup.sh          install / update
#   bash devagents-setup.sh check    show what is installed
# Needs: Debian/Ubuntu amd64, sudo. Login to WorkBuddy/CodeBuddy is interactive and done once by the user.
set -u
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
export DEBIAN_FRONTEND=noninteractive
LOG="$HOME/.config/aiw/devagents-setup.log"; mkdir -p "$(dirname "$LOG")"
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"

# REA needs Node ^22.23.4 || ^24.22.1 || >=26.12.1
node_ok() {
  v=$(node -v 2>/dev/null | sed 's/^v//') || return 1
  [ -n "$v" ] || return 1
  IFS=. read -r a b c <<<"$v"
  { [ "$a" -eq 22 ] && { [ "$b" -gt 23 ] || { [ "$b" -eq 23 ] && [ "$c" -ge 4 ]; }; }; } && return 0
  { [ "$a" -eq 24 ] && { [ "$b" -gt 22 ] || { [ "$b" -eq 22 ] && [ "$c" -ge 1 ]; }; }; } && return 0
  { [ "$a" -eq 26 ] && { [ "$b" -gt 12 ] || { [ "$b" -eq 12 ] && [ "$c" -ge 1 ]; }; }; } && return 0
  [ "$a" -gt 26 ] && return 0
  return 1
}

check() {
  echo "node      $(node -v 2>/dev/null || echo missing) $(node_ok && echo '(ok for REA)' || echo '(REA needs 22.23.4+/24.22.1+)')"
  echo "procps    $(command -v ps >/dev/null && ps --version 2>/dev/null | head -1 || echo missing)"
  echo "codebuddy $(codebuddy --version 2>/dev/null || echo missing)"
  echo "workbuddy $(dpkg-query -W -f='${Version}' workbuddy 2>/dev/null || echo missing)"
  if [ -f "$HOME/.workbuddy/mcp.json" ] && grep -q '"rea"' "$HOME/.workbuddy/mcp.json"; then echo "rea mcp   registered in ~/.workbuddy/mcp.json"; else echo "rea mcp   not registered"; fi
  echo "rea skill $( [ -d "$HOME/.workbuddy/skills" ] && ls "$HOME/.workbuddy/skills" 2>/dev/null | grep -i -m1 reverse || echo none)"
}
[ "${1:-}" = "check" ] && { check; exit 0; }

step() { echo "== $*" | tee -a "$LOG"; }

step "apt base (procps, curl, ca-certificates)"
$SUDO apt-get update -qq >>"$LOG" 2>&1
$SUDO apt-get install -y -qq procps curl ca-certificates gnupg xdg-utils >>"$LOG" 2>&1

if ! node_ok; then
  step "Node 24 LTS from NodeSource (have: $(node -v 2>/dev/null || echo none))"
  curl -fsSL https://deb.nodesource.com/setup_24.x | $SUDO -E bash - >>"$LOG" 2>&1
  $SUDO apt-get install -y -qq nodejs >>"$LOG" 2>&1
fi
# An older node earlier on PATH (InstaBox ships one in /usr/local/bin) shadows the apt one: point it at /usr/bin.
if ! node_ok && [ -x /usr/bin/node ]; then
  for b in node npm npx; do
    p=$(command -v $b); if [ -n "$p" ] && [ "$p" != "/usr/bin/$b" ] && [ -e "/usr/bin/$b" ]; then
      step "replace older $p with a link to /usr/bin/$b (backup $p.bak-old)"
      [ -L "$p" ] || $SUDO mv "$p" "$p.bak-old"; $SUDO ln -sf "/usr/bin/$b" "$p"
    fi
  done
  hash -r
fi
node_ok || { echo "FAILED: Node $(node -v 2>/dev/null) still too old for REA; see $LOG"; exit 1; }

step "CodeBuddy Code CLI (WorkBuddy 终端, command: codebuddy)"
$SUDO npm install -g @tencent-ai/codebuddy-code@latest >>"$LOG" 2>&1 || echo "WARN: codebuddy npm install failed, see $LOG"

step "WorkBuddy desktop (official Linux deb via Tencent update API)"
ARCH=$(dpkg --print-architecture); case "$ARCH" in amd64) P=x64;; arm64) P=arm64;; *) P="";; esac
if [ -n "$P" ]; then
  META=$(curl -fsSL -m 30 "https://copilot.tencent.com/v2/update?platform=workbuddy-linux-$P-deb")
  URL=$(printf '%s' "$META" | sed -n 's/.*"url":"\([^"]*\)".*/\1/p')
  SHA=$(printf '%s' "$META" | sed -n 's/.*"sha256hash":"\([^"]*\)".*/\1/p')
  VER=$(printf '%s' "$META" | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')
  HAVE=$(dpkg-query -W -f='${Version}' workbuddy 2>/dev/null || true)
  if [ -n "$URL" ] && [ "$HAVE" != "$VER" ]; then
    DEB=/tmp/workbuddy-$VER.deb
    curl -fL -m 900 -o "$DEB" "$URL" >>"$LOG" 2>&1
    if [ -n "$SHA" ] && ! echo "$SHA  $DEB" | sha256sum -c - >>"$LOG" 2>&1; then
      echo "FAILED: WorkBuddy deb checksum mismatch"; rm -f "$DEB"
    else
      $SUDO apt-get install -y -qq "$DEB" >>"$LOG" 2>&1 || echo "WARN: WorkBuddy deb install failed, see $LOG"
      rm -f "$DEB"
    fi
  else
    echo "   WorkBuddy ${HAVE:-?} is current"
  fi
else
  echo "WARN: no WorkBuddy Linux build for $ARCH"
fi

step "REA (npx rea-agents setup) for WorkBuddy"
mkdir -p "$HOME/.workbuddy"
npx -y rea-agents@latest setup --client workbuddy --yes >>"$LOG" 2>&1 \
  || echo "WARN: rea setup returned non-zero (often just 'no analysis engine yet'); see $LOG"
npx -y rea-agents@latest doctor --client workbuddy --json >"$HOME/.config/aiw/rea-doctor.json" 2>>"$LOG" || true

touch "$HOME/.config/aiw/devagents-ready-v1"
echo; check
echo
echo "Next (once, by hand in the desktop/noVNC): open WorkBuddy and sign in; run 'codebuddy' in a terminal and sign in."
