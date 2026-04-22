#!/usr/bin/env bash
# Client (load-generator) bootstrap. Installs Python 3 + git, clones
# the benchmark repo, creates a venv, installs the package and the
# full set of backend drivers. The orchestrator SSHes in after this
# and drives benchmarks/session_latency.py per variant.
set -euo pipefail

log() { echo "[$(date -u +%FT%TZ)] client: $*"; }

META="http://metadata.google.internal/computeMetadata/v1/instance/attributes"
HDR=(-H "Metadata-Flavor: Google")

REPO_URL=$(curl -fsS "${HDR[@]}" "${META}/repo-url")
REPO_REF=$(curl -fsS "${HDR[@]}" "${META}/repo-ref")

# --- OS baseline --------------------------------------------------------

echo never > /sys/kernel/mm/transparent_hugepage/enabled || true

cat > /etc/sysctl.d/99-bench.conf <<'CONF'
net.core.somaxconn = 4096
net.ipv4.tcp_fin_timeout = 15
net.ipv4.tcp_tw_reuse = 1
fs.file-max = 1000000
CONF
sysctl --system >/dev/null

for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
  [ -f "$g" ] && echo performance > "$g" || true
done

# Raise the per-process file descriptor limit for the user-level
# benchmark process. Concurrency sweeps up to 64 sessions each holding
# multiple connections across 3-node clusters will exhaust the 1024
# default.
cat > /etc/security/limits.d/99-bench.conf <<'CONF'
*  soft  nofile  65535
*  hard  nofile  65535
CONF

# --- Packages ----------------------------------------------------------

apt-get update
apt-get install -y python3 python3-venv python3-pip git curl ca-certificates build-essential \
  redis-tools postgresql-client

# --- Repo + venv -------------------------------------------------------

TARGET_USER=$(getent passwd 1000 | cut -d: -f1 || echo "ubuntu")
HOME_DIR=$(getent passwd "${TARGET_USER}" | cut -d: -f6)

sudo -u "${TARGET_USER}" env REPO_URL="${REPO_URL}" REPO_REF="${REPO_REF}" bash <<'EOS'
set -euo pipefail
cd "$HOME"
if [ ! -d aerospike-openai-agents ]; then
  git clone "$REPO_URL" aerospike-openai-agents
fi
cd aerospike-openai-agents
git fetch --all --tags
git checkout "$REPO_REF"
git pull --ff-only || true

python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -e .
pip install 'redis>=5.0' 'sqlalchemy>=2.0' 'asyncpg>=0.29' 'aiosqlite>=0.19'
EOS

log "Client ready. Repo at ${HOME_DIR}/aerospike-openai-agents @ ${REPO_REF}."
