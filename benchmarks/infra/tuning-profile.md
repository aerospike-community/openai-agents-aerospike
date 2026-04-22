# Benchmark tuning profile

**Status: LOCKED for the GCP sweep.** Accepted decisions are folded in
below; the original "open questions" section is now a sign-off record.
If a number needs to change, update it here first and retag
`benchmarks/infra/` so every published result references a definite
tuning state.

This document pairs with `benchmarks/infra/terraform/`: every knob
below is applied by a module startup script in that directory.
Comparing to vendor docs? Each non-default value names the vendor doc
it came from, so you can argue against the source rather than against
me.

## Accepted decisions (2026-04-21)

- **Two topologies, not one.** Every backend runs in both a
  single-node shape and a 3-node production shape, so the writeup can
  show both per-node ceiling and per-cluster behavior. That's the only
  way the story is complete.
- **Hardware parity across backends.** Every DB node (Aerospike,
  Redis, Postgres) is `n2d-standard-8` + 1x 375 GB local NVMe. Same
  machine class, same physical storage class. Aerospike uses raw
  partitions; Redis and Postgres format ext4 and mount the NVMe as
  their data dir so AOF / WAL fsyncs hit the same media.
- **NVMe partitioning (Aerospike).** Each 375 GB local NVMe is
  carved into 4 equal raw partitions (~93 GB each) and every partition
  is a separate `device` entry in `storage-engine`. Aerospike scales
  write / defrag parallelism with device count, so one big device
  underperforms N small ones on the same media. ([Aerospike SSD best
  practices](https://aerospike.com/docs/server/operations/plan/ssd))
- **Durability tiers.**
  - Aerospike headline: async flush (`commit-to-device=false`, vendor
    default) + RF=2 in the 3-node topology. Durability variant:
    `commit-to-device=true` on one variant of the sweep for a direct
    comparison.
  - Postgres headline: `synchronous_commit = on` (durable-per-commit
    on the primary, local WAL fsync). Durability variant:
    `synchronous_commit = off` (group commit — more liberal, pairs
    durability-wise with Redis `everysec`) on one variant of the
    sweep.
  - Redis: AOF + `appendfsync everysec`. No variant; this is how
    serious Redis deployments run.
- **Redis 3-node = Sentinel, not Cluster.** Cluster would require
  hash-tagged keys in upstream `RedisSession`
  (`session:{id}:messages`, etc.) which we cannot do without an
  upstream PR. Sentinel (1 primary + 2 replicas + 3 colocated
  sentinels, quorum 2) gives 3-node HA with zero client code changes
  and matches Postgres's primary-replica semantics.
- **Postgres 3-node = primary + 2 async streaming replicas.** No way
  around single-writer semantics without Citus / Patroni / similar;
  not in scope.
- **Client pool sizing.** All three clients sized above the highest
  sweep concurrency (64). Aerospike 300 conns/node; Redis 512
  connections; Postgres SQLAlchemy `pool_size=32` (bumped to match
  concurrency via `--sqlalchemy-pool-size` at run time).

## Asymmetry, acknowledged

The 3-node sweep is not symmetric and the writeup will say so:

| Backend | 3-node writes |
|---|---|
| Aerospike | Sharded across all 3 nodes (linear-ish write scaling) |
| Redis | Single-writer (primary), 2 async replicas |
| Postgres | Single-writer (primary), 2 async replicas |

Aerospike is the only one of the three whose production topology
scales writes horizontally. That's a true property of the
architectures, not a benchmark bug. Publishing both per-node and
per-cluster numbers lets readers form their own view of "fast because
of code" vs. "fast because of architecture".

## Hardware baseline

| Role | Instance | vCPU | RAM | Storage |
|---|---|---:|---:|---|
| Aerospike (1-node or 3-node) | `n2d-standard-8` | 8 | 32 GB | 1x 375 GB local NVMe, **4 raw partitions** |
| Redis (1-node or 3-node Sentinel) | `n2d-standard-8` | 8 | 32 GB | 1x 375 GB local NVMe, ext4, mounted at `/var/lib/redis` |
| Postgres (1-node or primary+2 replicas) | `n2d-standard-8` | 8 | 32 GB | 1x 375 GB local NVMe, ext4, mounted at data dir |
| Client | `n2d-standard-8` | 8 | 32 GB | 50 GB `pd-balanced` |

Single zone (`us-central1-a`) throughout. Client and DB nodes share
one VPC; RTT is in the 100-300 us band.

Local NVMe is ephemeral (wiped on stop). That's acceptable because
every 3-node topology tolerates one node loss via replication, and
single-node runs are one-shot sweeps anyway — the orchestrator
`terraform apply`s, runs the sweep, collects results, and `terraform
destroy`s. No data needs to survive a restart.

## OS baseline (applied to every VM)

| Knob | Value | Why |
|---|---|---|
| CPU governor | `performance` | All three vendor guides call for it. |
| Transparent huge pages | `never` | Redis warns loudly; Aerospike and Postgres prefer explicit hugepages. |
| `vm.swappiness` | `1` | Every service is I/O-bound; swapping = pure latency penalty. `0` is the most aggressive setting but `1` still hands the kernel an escape valve. |
| `net.core.somaxconn` | `4096` | Matches Redis `tcp-backlog` and Aerospike's listen queue. |
| `net.ipv4.tcp_tw_reuse` | `1` | Benchmark churns sockets under session rotation. |
| `fs.file-max` | `1000000` | Redis 10k maxclients + Aerospike FD cap + Postgres 200 conns + headroom. |
| `/etc/security/limits.d/*` nofile | `65535` | Client VM; per-process FD limit for 64-concurrency sweeps. |

Postgres VMs additionally set `kernel.shmmax` to 16 GB to accommodate
`shared_buffers = 8GB` with some headroom.

## Aerospike

### Storage layout

- Raw NVMe (no filesystem). Partition each 375 GB device into **4
  equal raw partitions** via `parted mklabel gpt` + 4 percentage-based
  partitions. List all 4 partitions as separate `device` entries in
  the `storage-engine`. Wiped with `wipefs` before partitioning so
  first boot and reboot behave identically.
- `chown root:aerospike` on each partition so the daemon can open
  them.

### `aerospike.conf`

```ini
service {
    feature-key-file /etc/aerospike/features.conf
    # service-threads left at nproc=8 (vendor default); documented
    # here so "default" doesn't drift silently.
    proto-fd-max 15000
}

logging {
    console { context any info }
}

network {
    service { address any; port 3000 }
    heartbeat {
        mode mesh
        port 3002
        # mesh-seed-address-port lines injected at provisioning
        # time, one per peer discovered via gcloud GCE listing.
        interval 150
        timeout 10
    }
    fabric { port 3001 }
    info   { port 3003 }
}

namespace bench {
    replication-factor 1  # single-node (clamped)
    # OR
    replication-factor 2  # 3-node (production-minimum)
    default-ttl 0
    nsup-period 120

    storage-engine device {
        device /dev/nvme0n1p1
        device /dev/nvme0n1p2
        device /dev/nvme0n1p3
        device /dev/nvme0n1p4
        write-block-size 128K
        defrag-lwm-pct 50
        post-write-queue 256
        high-water-disk-pct 80
        stop-writes-pct 90
        # commit-to-device true   # durability-variant sweep only
    }
}
```

Rationale for every non-default:

- **`replication-factor`** — 1 on single-node (physically can't
  replicate); 2 on 3-node (production-minimum, tolerates one node
  loss).
- **`post-write-queue 256`** — default is 0. Benchmark "write N
  messages then read all N back" pattern benefits from post-write
  cache; common vendor-recommended starting point for write-heavy
  workloads.
- **`write-block-size 128K`** — storage-engine default for modern
  SSD. Matches NVMe erase-block economics.
- **`defrag-lwm-pct 50`** — matches the vendor's "busy writer"
  recommendation; defaults are tuned for lightly-loaded systems.
- **`proto-fd-max 15000`** — vendor default, named explicitly so
  future drift is visible.

### Aerospike client tuning

Harness sets:

- `ClientPolicy.max_conns_per_node = 300`
- `ClientPolicy.min_conns_per_node = 100`
- `ClientPolicy.async_max_conns_per_node = 100`
- `ClientPolicy.conn_pools_per_node = 4`

Sized above the highest sweep concurrency (64) so pool waits stay out
of measured latency.

## Redis

### `redis.conf`

```ini
bind 0.0.0.0 -::*
protected-mode no
port 6379
tcp-backlog 4096
tcp-keepalive 60
timeout 0

maxclients 10000

io-threads 4
io-threads-do-reads yes

appendonly yes
appendfsync everysec
no-appendfsync-on-rewrite no
auto-aof-rewrite-percentage 100
auto-aof-rewrite-min-size 64mb
aof-use-rdb-preamble yes
save ""    # disable RDB snapshots; AOF is authoritative

maxmemory-policy noeviction

dir /var/lib/redis
```

### Replica + Sentinel (3-node topology only)

- Node 0 (`-redis-1`) is the initial primary.
- Nodes 1-2 boot with `replicaof <primary-ip> 6379`.
- Every node additionally runs `redis-sentinel` on port 26379 with
  `sentinel monitor bench-master <primary-ip> 6379 2`, `down-after
  5000`, `failover-timeout 30000`, `parallel-syncs 1`.

Rationale for every non-default:

- **`io-threads 4` + `io-threads-do-reads yes`** — on an 8-vCPU box
  the vendor recommends N/2 threads.
- **`save ""`** — disable RDB snapshots. AOF is authoritative; fork
  for snapshot is a p99 spike we don't want in the numbers.
- **`tcp-backlog 4096`** — has to be >= `net.core.somaxconn`.

### Redis client tuning

Harness sets `max_connections = 512` on the redis-py async client,
`socket_keepalive = True`, stock socket timeouts.

In 3-node / Sentinel deployments, the orchestrator resolves the
current primary (via `redis-cli -h <sentinel> SENTINEL
get-master-addr-by-name bench-master`) before the sweep and passes
that IP as `REDIS_URL`. Sweeps do not exercise failover, so the
primary IP is stable for the duration of the run; sentinel-aware
client logic is not required to produce comparable numbers.

## Postgres 16

### `postgresql.conf`

```ini
listen_addresses = '*'
port = 5432
max_connections = 200
password_encryption = scram-sha-256

shared_buffers = 8GB
effective_cache_size = 24GB
work_mem = 16MB
maintenance_work_mem = 2GB
huge_pages = try

wal_level = replica
wal_buffers = 16MB
max_wal_size = 8GB
min_wal_size = 2GB
wal_compression = on
checkpoint_timeout = 15min
checkpoint_completion_target = 0.9
synchronous_commit = on     # durability-variant flips this to off

max_wal_senders = 10
max_replication_slots = 10
hot_standby = on

random_page_cost = 1.1
effective_io_concurrency = 200

log_min_duration_statement = 500ms
log_checkpoints = on
log_lock_waits = on
log_temp_files = 0
```

### `pg_hba.conf`

- Local peer auth for `postgres`.
- VPC CIDR (`10.100.0.0/24`) allowed via `scram-sha-256` for both
  the `bench` role and the `replicator` role (replication).
- No `trust` auth.

### Replica bootstrap (3-node topology only)

1. Install Postgres package; drop the default cluster.
2. Format + mount local NVMe at the data dir.
3. Wait for `-postgres-1` (the primary) to accept connections.
4. Run `pg_basebackup -R` as the `replicator` role against the
   primary's data dir — `-R` writes `standby.signal` and records
   `primary_conninfo` in `postgresql.auto.conf`.
5. Overlay our `postgresql.conf` and `pg_hba.conf` on top of the
   baseBackup (without touching `postgresql.auto.conf`).
6. Start service; replica streams from the primary and serves reads
   in hot-standby mode.

Async replication (default streaming, no `synchronous_standby_names`).
Writes commit locally on the primary and replicate in the background;
this pairs with Postgres `synchronous_commit=on` which is local-disk
durability only.

### Postgres client tuning

SQLAlchemy async engine:

- `pool_size = 32` (overridable at run time; sized above concurrency).
- `max_overflow = pool_size`
- `pool_pre_ping = True`
- `asyncpg` driver.

## Durability matrix

| Backend | Headline sweep durability | Variant sweep |
|---|---|---|
| Aerospike | Async flush (`commit-to-device=false`) + RF=2 | `commit-to-device=true`, same sweep |
| Redis | AOF + `appendfsync everysec` | — |
| Postgres | `synchronous_commit = on` | `synchronous_commit = off` |
| SQLite | WAL mode, `synchronous = NORMAL` | — |

## Sign-off log

All five original open questions resolved 2026-04-21:

1. Aerospike `commit-to-device=false` as headline — **ACCEPTED** (variant sweep runs true).
2. Postgres `synchronous_commit = on` as headline — **ACCEPTED** (variant sweep runs off).
3. Redis `io-threads = 4` — **ACCEPTED**.
4. Single NVMe per Aerospike node with 4 raw partitions — **ACCEPTED** (partitioning is new; same physical-storage footprint as Redis / Postgres).
5. Client pool sizes above sweep concurrency — **ACCEPTED**.

Plus two decisions that were not in the original list:

6. Two topologies (single-node + three-node) — **ACCEPTED**.
7. Redis 3-node = Sentinel (not Cluster) — **ACCEPTED** (forced by upstream `RedisSession` pipeline keys lacking hash tags).
