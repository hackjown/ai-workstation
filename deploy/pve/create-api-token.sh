#!/usr/bin/env bash
# Least-privilege PVE API token for the VM Runtime (spec §19: AI never gets PVE root).
# Grants power + snapshot + audit on ONE VM only.
set -euo pipefail
VMID=${VMID:-100}
pveum role add AIWRuntime -privs "VM.Audit VM.PowerMgmt VM.Snapshot VM.Snapshot.Rollback VM.Monitor" 2>/dev/null || true
pveum user add aiw@pve --comment "AI Workstation VM Runtime" 2>/dev/null || true
pveum acl modify "/vms/$VMID" -user aiw@pve -role AIWRuntime
pveum user token add aiw@pve runtime --privsep 0 --comment "vm-runtime" 
echo "Put the token into .env as PVE_TOKEN_ID=aiw@pve!runtime and PVE_TOKEN_SECRET=<value above>"
