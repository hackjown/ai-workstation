#!/usr/bin/env bash
# Everyday conversion toolbox for an AI Workstation computer (Proxmox VM or InstaBox cloud machine):
# office documents, PDF, OCR, images, audio/video, e-books, archives, text encodings and the Python libraries
# the agent reaches for in scripts. task-a0e split an exam PDF by rasterising it because pypdf, qpdf and gs
# were missing, and could not install them (no pip).
#
#   toolbox-setup.sh          install whatever is missing (idempotent, safe to run again; takes several minutes)
#   toolbox-setup.sh status   one line: READY v<n> | INSTALLING <group> | NEEDS_SUDO | PARTIAL <failed packages> | NONE
#   toolbox-setup.sh check    which everyday commands are present / missing
#
# Every group is installed on its own and falls back to one package at a time, so a package a Debian release
# does not ship (names differ between bookworm and trixie) never blocks the rest. vm-agent starts it in the
# background on its first run when the marker is missing (sudo -n only); on the Proxmox VM, where sudo asks for
# a password, run it once by hand: bash ~/ai-workstation/deploy/vm/toolbox-setup.sh
set -u
VER=1
S=$HOME/.config/aiw
ST=$S/toolbox.status
LOG=$S/toolbox.log
MARK=$S/toolbox-ready-v$VER
mkdir -p "$S"

APT_GROUPS=(
  "office:libreoffice-writer libreoffice-calc libreoffice-impress libreoffice-draw libreoffice-gtk3 libreoffice-l10n-zh-cn libreoffice-help-zh-cn pandoc"
  "fonts:fonts-noto-cjk fonts-noto-color-emoji fonts-wqy-zenhei fonts-wqy-microhei fonts-arphic-ukai fonts-arphic-uming fonts-liberation2 fonts-crosextra-carlito fonts-crosextra-caladea fonts-dejavu"
  "pdf:poppler-utils qpdf ghostscript mupdf-tools pdftk-java img2pdf ocrmypdf python3-pypdf python3-fitz python3-pdfplumber python3-reportlab"
  "ocr:tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-chi-tra tesseract-ocr-eng"
  "image:imagemagick webp libheif-examples librsvg2-bin potrace optipng jpegoptim pngquant libimage-exiftool-perl inkscape python3-pil"
  "media:ffmpeg sox libsox-fmt-all lame flac opus-tools mediainfo yt-dlp"
  "ebook:calibre"
  "archive:zip unzip 7zip p7zip-full unrar-free xz-utils zstd cabextract"
  "text:jq dos2unix uchardet enca file csvkit xmlstarlet"
  "python:python3-pip python3-venv python3-openpyxl python3-xlrd python3-docx python3-pptx python3-pandas python3-matplotlib python3-lxml python3-bs4 python3-markdown python3-yaml python3-chardet python3-ezdxf python3-qrcode"
)
# not packaged (or too old) in Debian: user-level pip, never touching the system Python's own packages
# pdf2docx pulls numpy 2 into ~/.local, which breaks Debian 12 C extensions built on numpy 1: upgrade those too
PIP_PKGS="python-pptx pdf2docx pandas numexpr bottleneck matplotlib"
# fallback via pip only when the Debian package is missing (e.g. pdfplumber on Debian 12)
PIP_FALLBACK="pdfplumber:pdfplumber pandas:pandas fitz:pymupdf pypdf:pypdf reportlab:reportlab openpyxl:openpyxl docx:python-docx"

status() {
  if [ -f "$MARK" ]; then echo "READY v$VER"; elif [ -s "$ST" ]; then cat "$ST"; else echo NONE; fi
}

check() {
  for c in soffice pandoc pdftotext pdftoppm qpdf gs mutool pdftk img2pdf ocrmypdf tesseract convert cwebp \
           heif-convert rsvg-convert inkscape ffmpeg ffprobe sox lame flac mediainfo yt-dlp ebook-convert 7z zip unzip \
           unrar-free zstd jq dos2unix uchardet enca; do
    command -v "$c" >/dev/null && echo "ok      $c" || echo "missing $c"
  done
  for m in pypdf fitz pdfplumber reportlab PIL openpyxl xlrd docx pptx pandas matplotlib lxml bs4 markdown yaml ezdxf; do
    python3 -c "import $m" 2>/dev/null && echo "ok      python:$m" || echo "missing python:$m"
  done
}

run() {
  if [ "$(id -u)" = 0 ]; then SUDO=""; elif sudo -n true 2>/dev/null; then SUDO="sudo -n"
  elif [ -t 0 ]; then SUDO="sudo"; else echo NEEDS_SUDO > "$ST"; echo "sudo needs a password: run this script once in a terminal" >> "$LOG"; return 1; fi
  : > "$LOG"
  echo "INSTALLING apt-update" > "$ST"
  export DEBIAN_FRONTEND=noninteractive
  $SUDO env DEBIAN_FRONTEND=noninteractive apt-get update -qq >>"$LOG" 2>&1
  failed=""
  for g in "${APT_GROUPS[@]}"; do
    name=${g%%:*}; pkgs=${g#*:}
    echo "INSTALLING $name" > "$ST"; echo "== $name: $pkgs" >> "$LOG"
    # shellcheck disable=SC2086
    if ! $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends $pkgs >>"$LOG" 2>&1; then
      for p in $pkgs; do
        $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "$p" >>"$LOG" 2>&1 \
          || failed="$failed $p"
      done
    fi
  done
  echo "INSTALLING pip" > "$ST"
  if python3 -m pip --version >/dev/null 2>&1; then
    # shellcheck disable=SC2086
    for p in $PIP_PKGS; do python3 -m pip install -q --user --break-system-packages "$p" >>"$LOG" 2>&1 || true; done
    for mp in $PIP_FALLBACK; do
      python3 -c "import ${mp%%:*}" 2>/dev/null || python3 -m pip install -q --user --break-system-packages "${mp#*:}" >>"$LOG" 2>&1 || true
    done
  fi
  # ImageMagick's Debian policy refuses PDF/PS: allow them, the agent converts PDFs to images all the time
  for pol in /etc/ImageMagick-6/policy.xml /etc/ImageMagick-7/policy.xml; do
    [ -f "$pol" ] && $SUDO sed -i -E 's#(<policy domain="coder" rights=")none(" pattern="(PDF|PS|EPS|XPS)")#\1read|write\2#' "$pol"
  done
  fc-cache -f >/dev/null 2>&1 || true
  # a package some releases lack (unrar-free, p7zip-full vs 7zip, csvkit...) is fine as long as the core landed
  core_ok=1
  for c in soffice pdftotext qpdf gs tesseract ffmpeg convert 7z jq; do command -v "$c" >/dev/null || core_ok=0; done
  if [ "$core_ok" = 1 ]; then
    touch "$MARK"; echo "READY v$VER" > "$ST"
  else
    echo "PARTIAL${failed}" > "$ST"
  fi
  [ -n "$failed" ] && echo "not installed:$failed" >> "$LOG"
  cat "$ST"
}

case "${1:-install}" in
  status) status ;;
  check) check ;;
  install|"") run ;;
  *) echo "usage: $0 [install|status|check]"; exit 1 ;;
esac
