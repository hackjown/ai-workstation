#!/usr/bin/env bash
# Phase 0: reference repos (research only, never production dependencies - spec §13)
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p references
clone() { [ -d "references/$2" ] || git clone --depth 1 "$1" "references/$2"; }
clone https://github.com/trycua/cua.git cua
clone https://github.com/e2b-dev/open-computer-use.git e2b-open-computer-use
clone https://github.com/getnenai/dexbox.git dexbox
clone https://github.com/lineCode/open-computer-use.git open-computer-use
clone https://github.com/OpenHands/OpenHands.git openhands
# OpenHands' runtime/events/security code now lives in the SDK repo:
clone https://github.com/OpenHands/software-agent-sdk.git openhands-software-agent-sdk
