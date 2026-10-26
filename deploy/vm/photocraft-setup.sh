#!/usr/bin/env bash
# PhotoCraft (github.com/storytold/photocraft): Photoshop-like editor in Rust, plus photocraft-cli for
# headless edits. Installs the newest release .deb, the software Vulkan driver (no GPU on the VMs),
# and a desktop icon.  Usage: bash photocraft-setup.sh [check]
set -euo pipefail
if [ "${1:-}" = "check" ]; then photocraft-cli --version && exit 0 || exit 1; fi
case "$(dpkg --print-architecture)" in amd64) A=x86_64;; arm64) A=aarch64;; *) echo "unsupported arch"; exit 1;; esac
SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo"
URL=$(curl -fsSL https://api.github.com/repos/storytold/photocraft/releases/latest \
  | grep -o "https://[^\"]*photocraft-[0-9.]*-linux-$A\.deb" | head -1)
[ -n "$URL" ] || { echo "no .deb found for $A"; exit 1; }
echo "downloading $URL"
curl -fL --retry 3 -o /tmp/photocraft.deb "$URL"
$SUDO apt-get update -qq || true
$SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y /tmp/photocraft.deb mesa-vulkan-drivers libegl-mesa0 libvulkan1
rm -f /tmp/photocraft.deb
D="$HOME/Desktop"; mkdir -p "$D"
APP=$(ls /usr/share/applications/*photocraft*.desktop 2>/dev/null | head -1 || true)
if [ -n "$APP" ]; then cp "$APP" "$D/"; chmod +x "$D/$(basename "$APP")"
  command -v gio >/dev/null && gio set "$D/$(basename "$APP")" metadata::trusted true 2>/dev/null || true; fi
photocraft-cli --version
echo "OK: GUI = photocraft (DISPLAY=:0 or :1), headless = photocraft-cli run in.png --cmd ... --out out.png"
