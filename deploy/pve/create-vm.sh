#!/usr/bin/env bash
# Run on the Proxmox host as root. Creates VM 100 "ai-linux-001" (spec §20).
# Usage: ISO=local:iso/ubuntu-24.04.1-desktop-amd64.iso STORAGE=local-lvm ./create-vm.sh
set -euo pipefail
VMID=${VMID:-100}
NAME=${NAME:-ai-linux-001}
STORAGE=${STORAGE:-local-lvm}
ISO=${ISO:?set ISO=<storage>:iso/<ubuntu-24.04-desktop>.iso}
BRIDGE=${BRIDGE:-vmbr0}

qm create "$VMID" --name "$NAME" --ostype l26 \
  --cores 4 --sockets 1 --cpu host --memory 8192 --balloon 0 \
  --scsihw virtio-scsi-single --scsi0 "$STORAGE:50,ssd=1,discard=on,iothread=1" \
  --net0 "virtio,bridge=$BRIDGE" \
  --ide2 "$ISO,media=cdrom" --boot "order=scsi0;ide2" \
  --vga std --agent enabled=1 --machine q35 --bios ovmf \
  --efidisk0 "$STORAGE:1,efitype=4m,pre-enrolled-keys=0"
echo "VM $VMID created. Start it, install Ubuntu 24.04 Desktop (user: aiw), then run deploy/vm/setup-vm.sh inside the VM."
echo "After setup, take a clean snapshot:  qm snapshot $VMID clean --description 'fresh ai-linux-001'"
