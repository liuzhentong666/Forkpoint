#!/usr/bin/env bash
# First installation only; run after attaching the ECS instance RAM role.
set -euo pipefail
umask 022
if [[ $(id -u) != 0 ]]; then
  echo 'Run with sudo bash deploy/ecs/install.sh' >&2
  exit 1
fi
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
if [[ -e /opt/agentdb-state || -e /etc/systemd/system/agentdb-state.service ]]; then
  echo 'Existing installation found; refusing to overwrite. See the deployment runbook.' >&2
  exit 1
fi
command -v systemctl >/dev/null
python3 deploy/ecs/verify_bundle.py
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv ca-certificates
if ! id agentdb-state >/dev/null 2>&1; then
  useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin agentdb-state
fi
install -d -m 0755 /opt/agentdb-state
python3 - <<'PY'
import json
import shutil
from pathlib import Path
root = Path('/opt/agentdb-state')
names = list(json.loads(Path('MANIFEST.json').read_text())['files']) + ['MANIFEST.json']
for name in names:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(name, target)
    target.chmod(0o644)
PY
cd /opt/agentdb-state
python3 -m venv .venv
.venv/bin/python -m pip install --no-cache-dir -r requirements-ecs.txt
runuser -u agentdb-state -- env AGENTDB_OSS_CREDENTIALS=ecs_ram_role PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m agent_state.server --config deploy/ecs/cloud.ecs.json --check
runuser -u agentdb-state -- env AGENTDB_OSS_CREDENTIALS=ecs_ram_role PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python smoke_cloud.py --config deploy/ecs/cloud.ecs.json
install -m 0644 deploy/ecs/agentdb-state.service /etc/systemd/system/agentdb-state.service
systemctl daemon-reload
systemctl enable --now agentdb-state
echo 'Service registered. Verify journal, HTTP response and an existing task after restart as documented.'
