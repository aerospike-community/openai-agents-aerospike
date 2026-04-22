#!/usr/bin/env bash
# run_sweep.sh — drive the benchmark harness against an applied terraform topology.
#
# What this does, in order:
#   1. Reads `terraform output -json` from the chosen topology root.
#   2. For each backend in the selected pass, SSHes into the client VM and
#      runs `benchmarks/session_latency.py` with that backend's connection
#      env vars + variant flags.
#   3. scp's the resulting JSON back to local, into a timestamped results
#      directory under benchmarks/results/gcp/.
#   4. Writes manifest.json describing what ran (topology, pass, tag,
#      backends, terraform-output snapshot, status per backend).
#
# What this intentionally does NOT do:
#   * apply/destroy terraform (that's your call; this script assumes the
#     infra is already up)
#   * run durability variants by reconfiguring DB tuning (you re-apply
#     with different tfvars, then re-run this script with --tag=durability-*)
#   * parse results into charts (see summarize_sweep.py)
#
# Usage:
#   ./run_sweep.sh <topology> <pass> [--tag=NAME] [--backends=a,b,c] [--dry-run]
#
#   topology:  single-node | three-node   (maps to benchmarks/infra/terraform/<topology>)
#   pass:      sanity | full              (matrix size — see PASS_* below)
#   --tag:     free-form label stamped into manifest + output dir name (default: headline)
#   --backends: override backend list (comma-sep: aerospike,aerospike-sharded,redis,sqlalchemy,sqlite)
#   --dry-run: print the commands it would run, don't SSH

set -euo pipefail

# --- arg parsing -------------------------------------------------------

if [ $# -lt 2 ]; then
  sed -n '3,30p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
fi

TOPOLOGY="$1"
PASS="$2"
shift 2

TAG="headline"
BACKENDS_OVERRIDE=""
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --tag=*)       TAG="${1#--tag=}";;
    --backends=*)  BACKENDS_OVERRIDE="${1#--backends=}";;
    --dry-run)     DRY_RUN=1;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
  shift
done

case "$TOPOLOGY" in
  # aerolab-compare is a read-only topology: it provisions only a client VM
  # and points it at an externally-managed (aerolab-built) Aerospike cluster.
  # It therefore only supports the aerospike/aerospike-sharded backends.
  single-node|three-node|aerolab-compare) :;;
  *) echo "topology must be single-node, three-node, or aerolab-compare (got: $TOPOLOGY)" >&2; exit 2;;
esac

case "$PASS" in
  sanity|full) :;;
  *) echo "pass must be sanity or full (got: $PASS)" >&2; exit 2;;
esac

# --- repo paths --------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
TF_DIR="${REPO_ROOT}/benchmarks/infra/terraform/${TOPOLOGY}"

if [ ! -f "${TF_DIR}/main.tf" ]; then
  echo "terraform root not found: ${TF_DIR}" >&2
  exit 3
fi

# --- variant matrix ----------------------------------------------------
#
# Keep these small lists explicit. The harness already handles the
# matrix within a single invocation (comma-separated --concurrency and
# --history-depth), so one SSH per backend covers all variants for that
# backend.

PASS_SANITY_BACKENDS="aerospike redis sqlalchemy"
PASS_SANITY_CONCURRENCY="1,4,16"
PASS_SANITY_DEPTHS="0,50"
PASS_SANITY_ITERS=100
PASS_SANITY_WARMUP=25

PASS_FULL_BACKENDS="aerospike aerospike-sharded redis sqlalchemy sqlite"
PASS_FULL_CONCURRENCY="1,4,16,64"
PASS_FULL_DEPTHS="0,50,200"
PASS_FULL_ITERS=500
PASS_FULL_WARMUP=50

if [ "$PASS" = "sanity" ]; then
  DEFAULT_BACKENDS="$PASS_SANITY_BACKENDS"
  CONCURRENCY="$PASS_SANITY_CONCURRENCY"
  DEPTHS="$PASS_SANITY_DEPTHS"
  ITERS="$PASS_SANITY_ITERS"
  WARMUP="$PASS_SANITY_WARMUP"
else
  DEFAULT_BACKENDS="$PASS_FULL_BACKENDS"
  CONCURRENCY="$PASS_FULL_CONCURRENCY"
  DEPTHS="$PASS_FULL_DEPTHS"
  ITERS="$PASS_FULL_ITERS"
  WARMUP="$PASS_FULL_WARMUP"
fi

if [ -n "$BACKENDS_OVERRIDE" ]; then
  BACKENDS="${BACKENDS_OVERRIDE//,/ }"
else
  BACKENDS="$DEFAULT_BACKENDS"
fi

# --- read terraform outputs -------------------------------------------

echo "[orch] reading terraform outputs from ${TF_DIR}..."
pushd "$TF_DIR" >/dev/null
if ! terraform output -json >/tmp/tf-out.$$.json 2>/tmp/tf-err.$$; then
  echo "[orch] terraform output failed:" >&2
  cat /tmp/tf-err.$$ >&2
  rm -f /tmp/tf-err.$$
  exit 4
fi
popd >/dev/null

TF_OUT="/tmp/tf-out.$$.json"

CLIENT_NAME=$(jq -r '.client_name.value' "$TF_OUT")
AS_SEED=$(jq -r '.aerospike_seed_ip.value' "$TF_OUT")
REDIS_URL=$(jq -r '.redis_url.value // empty' "$TF_OUT")
SQLA_URL=$(jq -r '.sqlalchemy_url.value // empty' "$TF_OUT")
TOPO_OUT=$(jq -r '.topology.value' "$TF_OUT")
ZONE=$(jq -r '.zone.value // empty' "$TF_OUT")
[ -z "$ZONE" ] && ZONE="us-central1-a"

# Namespace is always 'bench' for our own terraform, but the aerolab-compare
# topology points at an external cluster whose namespace we don't control
# (aerolab's default is 'test'). Pick it up from terraform output if present.
AS_NAMESPACE=$(jq -r '.aerospike_namespace.value // empty' "$TF_OUT")
[ -z "$AS_NAMESPACE" ] && AS_NAMESPACE="bench"

if [ -z "$CLIENT_NAME" ] || [ "$CLIENT_NAME" = "null" ]; then
  echo "[orch] terraform hasn't been applied (no client_name output). Run 'terraform apply' in ${TF_DIR} first." >&2
  exit 5
fi

if [ "$TOPO_OUT" != "$TOPOLOGY" ]; then
  echo "[orch] sanity check failed: terraform reports topology=$TOPO_OUT but script was invoked with $TOPOLOGY" >&2
  exit 6
fi

echo "[orch] topology=${TOPOLOGY} zone=${ZONE}"
echo "[orch] client=${CLIENT_NAME}"
echo "[orch] aerospike_seed=${AS_SEED} namespace=${AS_NAMESPACE}"
[ -n "$REDIS_URL" ] && echo "[orch] redis_url=${REDIS_URL}"
[ -n "$SQLA_URL" ]  && echo "[orch] sqlalchemy_url=<redacted>"

# aerolab-compare only serves the aerospike/aerospike-sharded backends. Trim
# the backend list so we don't SSH in and immediately fail on a missing
# REDIS_URL or SQLALCHEMY_URL.
if [ "$TOPOLOGY" = "aerolab-compare" ]; then
  TRIMMED=""
  for b in $BACKENDS; do
    case "$b" in
      aerospike|aerospike-sharded) TRIMMED="${TRIMMED} $b";;
      *) echo "[orch] skipping ${b}: aerolab-compare only supports aerospike backends";;
    esac
  done
  BACKENDS="${TRIMMED# }"
fi

# --- results dir -------------------------------------------------------

TS=$(date -u +%Y%m%dT%H%M%SZ)
RESULTS_DIR="${REPO_ROOT}/benchmarks/results/gcp/${TS}-${TOPOLOGY}-${PASS}-${TAG}"
mkdir -p "$RESULTS_DIR"
echo "[orch] results -> ${RESULTS_DIR}"

# --- per-backend runner ------------------------------------------------

run_backend() {
  local backend="$1"
  local remote_out="/tmp/bench-${backend}-${TS}.json"
  local local_out="${RESULTS_DIR}/${backend}.json"

  # Backend-specific env vars. The harness reads these and falls back
  # to its --*-host / --*-url flags if unset, but env vars are the
  # source of truth in tuning-profile.md.
  local env_prefix=""
  case "$backend" in
    aerospike|aerospike-sharded)
      # Namespace is topology-dependent: our own terraform modules configure
      # 'bench'; aerolab-compare points at an external cluster (aerolab default
      # 'test'). Plumbed through terraform output `aerospike_namespace`.
      env_prefix="AEROSPIKE_HOST='${AS_SEED}' AEROSPIKE_PORT=3000 AEROSPIKE_NAMESPACE='${AS_NAMESPACE}'"
      ;;
    redis)
      env_prefix="REDIS_URL='${REDIS_URL}'"
      ;;
    sqlalchemy)
      env_prefix="SQLALCHEMY_URL='${SQLA_URL}'"
      ;;
    sqlite)
      # sqlite is a local-only baseline. No external connection.
      env_prefix=""
      ;;
    *)
      echo "[orch] unknown backend: $backend" >&2
      return 10
      ;;
  esac

  # Concurrency sqlalchemy-pool-size needs to be >= max concurrency or
  # pooled connections will serialize requests and we'll measure pool
  # wait time instead of Postgres latency. Set pool size = max(concurrency).
  local max_conc
  max_conc=$(echo "$CONCURRENCY" | tr ',' '\n' | sort -n | tail -1)
  local extra_flags=""
  if [ "$backend" = "sqlalchemy" ]; then
    extra_flags="--sqlalchemy-pool-size=${max_conc}"
  fi

  # The client's startup script clones + sets up the venv under the
  # uid=1000 user (usually 'ubuntu' on GCP Ubuntu images). Our SSH
  # user may be different (GCP auto-provisions a per-invoker account),
  # so run the harness explicitly as 'ubuntu' via sudo. Absolute
  # paths only; no $HOME games.
  # One long single-line command avoids line-continuation pitfalls when
  # the string is marshalled through gcloud/ssh/bash-c. Keep the quoting
  # simple: single-quote the inner bash -c body, let ${env_prefix} and
  # flags interpolate at outer script time.
  local inner_cmd="cd /home/ubuntu/aerospike-openai-agents && . .venv/bin/activate && ${env_prefix} python benchmarks/session_latency.py --backend=${backend} --concurrency=${CONCURRENCY} --history-depth=${DEPTHS} --iterations=${ITERS} --warmup=${WARMUP} --output=${remote_out} ${extra_flags}"
  local remote_cmd="sudo -u ubuntu bash -c \"${inner_cmd}\""

  echo ""
  echo "[orch] === backend: ${backend} ==="
  if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] would ssh:"
    echo "${remote_cmd}"
    return 0
  fi

  local start
  start=$(date +%s)
  # ServerAliveInterval: gcloud ssh default doesn't send keepalive, so long
  # aerospike/aerospike-sharded runs at C=64 with big overload retries
  # (tens of thousands of retries over minutes) cause the session to
  # silently drop mid-benchmark and the orchestrator reports the whole
  # backend as FAILED. Inject an ssh keepalive so the connection survives
  # any single-variant burst.
  if gcloud compute ssh "$CLIENT_NAME" --zone="$ZONE" \
      --ssh-flag="-o ServerAliveInterval=60" \
      --ssh-flag="-o ServerAliveCountMax=10" \
      --command="$remote_cmd"; then
    gcloud compute scp --zone="$ZONE" "${CLIENT_NAME}:${remote_out}" "$local_out"
    local elapsed=$(( $(date +%s) - start ))
    echo "[orch] ${backend} OK (${elapsed}s) -> ${local_out}"
    echo "ok ${elapsed}" > "${RESULTS_DIR}/.status-${backend}"
  else
    local elapsed=$(( $(date +%s) - start ))
    echo "[orch] ${backend} FAILED after ${elapsed}s" >&2
    echo "fail ${elapsed}" > "${RESULTS_DIR}/.status-${backend}"
    return 11
  fi
}

# --- main loop ---------------------------------------------------------

OVERALL_START=$(date -u +%FT%TZ)
FAILED=()
for backend in $BACKENDS; do
  if ! run_backend "$backend"; then
    FAILED+=("$backend")
  fi
done
OVERALL_END=$(date -u +%FT%TZ)

# --- manifest ----------------------------------------------------------

GIT_SHA=$(cd "$REPO_ROOT" && git rev-parse HEAD)
GIT_BRANCH=$(cd "$REPO_ROOT" && git rev-parse --abbrev-ref HEAD)

jq -n \
  --arg topology   "$TOPOLOGY" \
  --arg pass       "$PASS" \
  --arg tag        "$TAG" \
  --arg started    "$OVERALL_START" \
  --arg ended      "$OVERALL_END" \
  --arg git_sha    "$GIT_SHA" \
  --arg git_branch "$GIT_BRANCH" \
  --arg client     "$CLIENT_NAME" \
  --arg zone       "$ZONE" \
  --arg concurrency "$CONCURRENCY" \
  --arg depths     "$DEPTHS" \
  --argjson iters  "$ITERS" \
  --argjson warmup "$WARMUP" \
  --arg backends   "$BACKENDS" \
  --arg failed     "${FAILED[*]:-}" \
  --slurpfile tf   "$TF_OUT" \
  '{
    topology: $topology,
    pass: $pass,
    tag: $tag,
    started_utc: $started,
    ended_utc: $ended,
    git: { sha: $git_sha, branch: $git_branch },
    client: { name: $client, zone: $zone },
    variants: {
      concurrency: $concurrency,
      history_depths: $depths,
      iterations: $iters,
      warmup: $warmup,
    },
    backends_requested: ($backends | split(" ")),
    backends_failed: (if $failed == "" then [] else ($failed | split(" ")) end),
    terraform_outputs: $tf[0],
  }' > "${RESULTS_DIR}/manifest.json"

# Scrub sensitive fields (sqlalchemy_url, postgres_password) from the
# committed manifest. Orchestrator only persisted them in memory; jq
# wrote terraform_outputs raw, which includes the sensitive values.
jq 'del(.terraform_outputs.sqlalchemy_url, .terraform_outputs.postgres_password)' \
  "${RESULTS_DIR}/manifest.json" > "${RESULTS_DIR}/manifest.json.tmp"
mv "${RESULTS_DIR}/manifest.json.tmp" "${RESULTS_DIR}/manifest.json"

rm -f "$TF_OUT"

# --- summary ----------------------------------------------------------

echo ""
echo "[orch] ==================================================="
echo "[orch] sweep complete: ${RESULTS_DIR}"
echo "[orch] requested: ${BACKENDS}"
if [ ${#FAILED[@]} -gt 0 ]; then
  echo "[orch] failed:    ${FAILED[*]}"
else
  echo "[orch] all backends OK"
fi
echo "[orch] next: python benchmarks/infra/orchestrator/summarize_sweep.py ${RESULTS_DIR}"
echo "[orch] ==================================================="

exit ${#FAILED[@]}
