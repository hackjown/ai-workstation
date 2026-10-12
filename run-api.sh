#!/usr/bin/env bash
# Start the API + web console on the API host.  ./run-api.sh  -> http://0.0.0.0:8000/?token=$AIW_API_TOKEN
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
export PYTHONPATH="protocols:services/computer-runtime:services/vm-runtime:services/tool-router:services/llm-router:services/agent-runtime:services/api:vm-agent"
exec uvicorn aiw_api.main:app --host "${AIW_HOST:-0.0.0.0}" --port "${AIW_PORT:-8000}"
