#!/usr/bin/env bash
# 照图出图 (cad-drawing) skill: SKILL.md, drawkit.py, drawauto.py, drawtrace.py and the worked example into
# ~/.claude/skills/cad-drawing, plus numpy / Pillow / ezdxf / OpenCV (headless) / RapidOCR and a Chinese font
# (drawkit.py ensure). The agent's drawing_build installs the same kit into ~/workspace/.aiw/cad-drawing by itself,
# so this script is optional for the drawing itself.
#   bash cad-drawing-setup.sh          install / update the kit and its Python dependencies
#   bash cad-drawing-setup.sh dwg      also install ODA File Converter (DXF -> DWG, free) + xvfb, on THIS VM only
#   bash cad-drawing-setup.sh check    exit 0 when the kit is usable
# Everything lives on the VM: nothing here goes into the API's Docker image.
set -euo pipefail
D="$HOME/.claude/skills/cad-drawing"
if [ "${1:-}" = "check" ]; then test -f "$D/drawkit.py" && python3 -c "import ezdxf, PIL, numpy" 2>/dev/null && exit 0 || exit 1; fi

install_dwg() {
  # ODA File Converter: Linux x86-64 only. Prefer the .deb (installs /usr/bin/ODAFileConverter); without root, the
  # AppImage is unpacked into ~/tools/ODAFileConverter (no FUSE needed). Override the URL with ODA_URL_DEB/ODA_URL_APPIMAGE.
  local base="https://www.opendesign.com/guestfiles/get?filename="
  local deb="${ODA_URL_DEB:-${base}ODAFileConverter_QT6_lnxX64_11dll.deb}"
  local app="${ODA_URL_APPIMAGE:-${base}ODAFileConverter_QT6_lnxX64_11dll.AppImage}"
  if [ "$(uname -m)" != "x86_64" ]; then echo "ODA File Converter needs x86_64 (this is $(uname -m)); DWG export stays off"; return 0; fi
  local SUDO=""; [ "$(id -u)" = "0" ] || { command -v sudo >/dev/null && sudo -n true 2>/dev/null && SUDO="sudo"; }
  if [ -n "$SUDO" ] || [ "$(id -u)" = "0" ]; then
    $SUDO apt-get update -qq || true
    $SUDO apt-get install -y -qq xvfb xauth libxkbcommon-x11-0 libxcb-cursor0 >/dev/null || true
    if curl -fsSL --retry 3 -o /tmp/oda.deb "$deb"; then
      $SUDO apt-get install -y -qq /tmp/oda.deb >/dev/null && echo "ODA File Converter installed (deb)" && return 0
    fi
  fi
  mkdir -p "$HOME/tools"
  curl -fsSL --retry 3 -o "$HOME/tools/ODAFileConverter.AppImage" "$app"
  chmod +x "$HOME/tools/ODAFileConverter.AppImage"
  (cd "$HOME/tools" && rm -rf squashfs-root ODAFileConverter && ./ODAFileConverter.AppImage --appimage-extract >/dev/null \
     && mv squashfs-root ODAFileConverter)
  echo "ODA File Converter unpacked into $HOME/tools/ODAFileConverter (xvfb-run is used when there is no display)"
}

BASE="${AIW_RAW:-https://raw.githubusercontent.com/hackjown/ai-workstation/main}/skills/cad-drawing"
LOCAL="$HOME/ai-workstation/skills/cad-drawing"
mkdir -p "$D/examples"
for f in SKILL.md drawkit.py drawauto.py drawtrace.py examples/xinda-g-108.spec.json; do
  if ! curl -fsSL --retry 3 --connect-timeout 15 -o "$D/$f.tmp" "$BASE/$f"; then
    [ -f "$LOCAL/$f" ] || { echo "cannot fetch $f"; rm -f "$D/$f.tmp"; exit 1; }
    cp "$LOCAL/$f" "$D/$f.tmp"
  fi
  mv "$D/$f.tmp" "$D/$f"
done
chmod +x "$D/drawkit.py"
python3 "$D/drawkit.py" ensure || echo "some dependencies are missing (see above); drawkit tries again on first use"
[ "${1:-}" = "dwg" ] && install_dwg
echo "cad-drawing installed in $D"
