#!/usr/bin/env bash
# Aerospike EE bootstrap. Partitions each local NVMe into N equal
# raw partitions and lists them as separate `device` entries in
# aerospike.conf — Aerospike scales write/defrag parallelism with
# device count, so one big 375 GB device underperforms four ~93 GB
# partitions on the same NVMe.
#
# References:
# - https://aerospike.com/docs/server/operations/plan/ssd
# - https://aerospike.com/docs/server/operations/configure/namespace/storage
set -euo pipefail

log() { echo "[$(date -u +%FT%TZ)] aerospike: $*"; }

META="http://metadata.google.internal/computeMetadata/v1/instance/attributes"
HDR=(-H "Metadata-Flavor: Google")

NODE_INDEX=$(curl -fsS "${HDR[@]}" "${META}/node-index")
CLUSTER_SIZE=$(curl -fsS "${HDR[@]}" "${META}/cluster-size")
VERSION=$(curl -fsS "${HDR[@]}" "${META}/aerospike-version")
NAMESPACE=$(curl -fsS "${HDR[@]}" "${META}/aerospike-namespace")
REPLICATION_FACTOR=$(curl -fsS "${HDR[@]}" "${META}/replication-factor")
LOCAL_SSD_COUNT=$(curl -fsS "${HDR[@]}" "${META}/local-ssd-count")
PARTITIONS_PER_SSD=$(curl -fsS "${HDR[@]}" "${META}/device-partitions-per-ssd")
COMMIT_TO_DEVICE=$(curl -fsS "${HDR[@]}" "${META}/commit-to-device")
FEATURES_B64=$(curl -fsS "${HDR[@]}" "${META}/features-conf-base64")

log "Node ${NODE_INDEX}/${CLUSTER_SIZE}, Aerospike EE ${VERSION}"

# --- OS baseline --------------------------------------------------------

# THP off (benchmark decision #OS baseline in tuning-profile.md).
echo never > /sys/kernel/mm/transparent_hugepage/enabled || true
echo never > /sys/kernel/mm/transparent_hugepage/defrag  || true

cat > /etc/sysctl.d/99-bench.conf <<'CONF'
net.core.somaxconn = 4096
net.ipv4.tcp_tw_reuse = 1
vm.swappiness = 1
fs.file-max = 1000000
CONF
sysctl --system >/dev/null

# Pin CPU governor explicitly; defend against image drift.
for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
  [ -f "$g" ] && echo performance > "$g" || true
done

# --- Install Aerospike EE -----------------------------------------------

apt-get update
apt-get install -y curl gnupg lsb-release python3 ca-certificates parted

# Aerospike EE 7.x ships as a combined server + tools tarball, not a
# standalone deb. Extract both debs and install together.
#
# The tools version (currently 11.1.0) is bundled with the server release
# and usually only changes across server minor versions; keep it in the
# URL rather than try to auto-discover.
TOOLS_VERSION=$(curl -fsS "${HDR[@]}" "${META}/aerospike-tools-version")
TGZ_URL="https://download.aerospike.com/artifacts/aerospike-server-enterprise/${VERSION}/aerospike-server-enterprise_${VERSION}_tools-${TOOLS_VERSION}_ubuntu22.04_x86_64.tgz"
curl -fsSL -o /tmp/aerospike.tgz "${TGZ_URL}"
mkdir -p /tmp/aerospike
tar -xzf /tmp/aerospike.tgz -C /tmp/aerospike --strip-components=1
apt-get install -y /tmp/aerospike/aerospike-server-enterprise_*.deb /tmp/aerospike/aerospike-tools_*.deb

mkdir -p /etc/aerospike
echo "${FEATURES_B64}" | base64 -d > /etc/aerospike/features.conf
chmod 640 /etc/aerospike/features.conf
chown root:aerospike /etc/aerospike/features.conf || true

# --- Partition each local NVMe -----------------------------------------
# parted creates an aligned GPT and N equal-size partitions per device.
# Each partition becomes a separate storage-engine `device` entry,
# which Aerospike parallelizes across.

DEVICE_LINES=""
for i in $(seq 0 $((LOCAL_SSD_COUNT - 1))); do
  # Local SSDs on n2d present as /dev/nvme0nN starting at nvme0n1
  SSD="/dev/nvme0n$((i + 1))"

  log "Partitioning ${SSD} into ${PARTITIONS_PER_SSD} equal parts"

  # Wipe any existing signature, write a new GPT, then carve equal
  # partitions. Percentages keep the math simple and alignment clean.
  wipefs -af "${SSD}" || true
  parted -s "${SSD}" mklabel gpt
  step=$((100 / PARTITIONS_PER_SSD))
  for p in $(seq 1 "${PARTITIONS_PER_SSD}"); do
    start=$(( (p - 1) * step ))
    end=$(( p * step ))
    # Last partition gets the remainder so rounding doesn't lose bytes.
    if [ "${p}" -eq "${PARTITIONS_PER_SSD}" ]; then end=100; fi
    parted -s "${SSD}" mkpart "as-${i}-${p}" "${start}%" "${end}%"
  done
  partprobe "${SSD}" || true

  # Wait for partition device nodes to appear before referencing them.
  for p in $(seq 1 "${PARTITIONS_PER_SSD}"); do
    for _ in $(seq 1 30); do
      [ -b "${SSD}p${p}" ] && break
      sleep 1
    done
    DEVICE_LINES="${DEVICE_LINES}        device ${SSD}p${p}"$'\n'
    # Aerospike wants raw ownership of these block devices.
    chown root:aerospike "${SSD}p${p}" || true
  done
done

# --- Peer discovery for mesh heartbeat ---------------------------------
# Find all aerospike-tagged instances in the project and add their
# internal IPs as mesh seeds. Works for node_count == 1 (no peers) too.
PEER_IPS=$(gcloud compute instances list \
  --filter="tags.items=aerospike" \
  --format='value(networkInterfaces[0].networkIP)' 2>/dev/null || true)

SEED_LINES=""
for ip in ${PEER_IPS}; do
  SEED_LINES="${SEED_LINES}        mesh-seed-address-port ${ip} 3002"$'\n'
done

# --- commit-to-device toggle -------------------------------------------
COMMIT_LINE=""
if [ "${COMMIT_TO_DEVICE}" = "true" ]; then
  COMMIT_LINE="        commit-to-device true"
fi

# --- aerospike.conf ----------------------------------------------------

cat > /etc/aerospike/aerospike.conf <<CONF
# Generated by benchmarks/infra startup script. Do not hand-edit;
# change the tuning profile or the module variables instead.
service {
    cluster-name bench
    feature-key-file /etc/aerospike/features.conf
    proto-fd-max 15000
}

logging {
    console {
        context any info
    }
}

network {
    service {
        address any
        port 3000
    }
    heartbeat {
        mode mesh
        port 3002
${SEED_LINES%$'\n'}
        interval 150
        timeout 10
    }
    fabric {
        port 3001
    }
    info {
        port 3003
    }
}

namespace ${NAMESPACE} {
    replication-factor ${REPLICATION_FACTOR}
    default-ttl 0
    nsup-period 120

    storage-engine device {
${DEVICE_LINES%$'\n'}
        # Aerospike 7.x reworked the storage-engine config: most of the
        # 6.x pct-based knobs (high-water-disk-pct, stop-writes-pct,
        # write-block-size) were removed or renamed. We keep the two
        # that 7.x still accepts and that matter for SSD workloads:
        #   flush-size: how big each flush IO is (tune per device)
        #   defrag-lwm-pct: when to reclaim partially-used blocks
        # Everything else — eviction, post-write cache, etc — uses the
        # sensible 7.x defaults. Revisit after a baseline run if we
        # see defrag pressure or eviction surprises in the telemetry.
        flush-size 128K
        defrag-lwm-pct 50
${COMMIT_LINE}
    }
}
CONF

systemctl enable aerospike
systemctl start aerospike

log "Aerospike started. Devices:"
echo "${DEVICE_LINES}" | sed 's/^/  /'
