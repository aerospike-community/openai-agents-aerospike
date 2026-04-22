"""Summarize a benchmark sweep into a human-readable markdown table.

Reads every ``<backend>.json`` in a sweep results directory and emits a
markdown report with one row per (backend, history_depth, concurrency)
variant and columns for p50 / p95 / p99 / mean turn latency. The report
is written to ``<results_dir>/SUMMARY.md`` and also printed to stdout so
it can be piped into ``gh pr comment`` or reviewed in the terminal.

This is a read-only post-processor; it never talks to the harness or
GCP. Run it locally after ``run_sweep.sh`` has scp'd the JSON files
back.

Usage::

    python benchmarks/infra/orchestrator/summarize_sweep.py \\
        benchmarks/results/gcp/20260421T120000Z-single-node-sanity-headline/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _load_manifest(results_dir: Path) -> dict[str, Any]:
    manifest_path = results_dir / "manifest.json"
    if not manifest_path.exists():
        sys.exit(f"no manifest.json in {results_dir} (is this a sweep directory?)")
    with manifest_path.open() as fh:
        return json.load(fh)


def _load_backend_results(results_dir: Path) -> dict[str, dict[str, Any]]:
    """Return {backend_name: parsed_json} for every ``<backend>.json`` in the dir."""
    results: dict[str, dict[str, Any]] = {}
    for path in sorted(results_dir.glob("*.json")):
        if path.name == "manifest.json":
            continue
        backend = path.stem
        try:
            with path.open() as fh:
                results[backend] = json.load(fh)
        except json.JSONDecodeError as exc:
            print(f"warning: failed to parse {path}: {exc}", file=sys.stderr)
    return results


def _fmt_ms(ms: float | None) -> str:
    """Render milliseconds with 2 decimals, or '—' if missing/NaN."""
    if ms is None:
        return "—"
    try:
        if ms != ms:
            return "—"
    except TypeError:
        return "—"
    return f"{ms:.2f}"


def _extract_rows(backend: str, blob: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk a harness output JSON and emit one row per variant.

    Harness emits (per variant)::

        {
          "history_depth_before_bench": int,
          "concurrency": int,
          "throughput_turns_per_second": float,
          "summary": {
            "turn":              {"p50_ms", "p95_ms", "p99_ms", "mean_ms", ...},
            "get_items_limit_20": {...},
            "add_items_2":       {...},
          },
          ...
        }
    """
    rows: list[dict[str, Any]] = []
    for v in blob.get("variants", []):
        turn = (v.get("summary") or {}).get("turn", {})
        rows.append(
            {
                "backend": backend,
                "depth": v.get("history_depth_before_bench"),
                "concurrency": v.get("concurrency"),
                "p50_ms": turn.get("p50_ms"),
                "p95_ms": turn.get("p95_ms"),
                "p99_ms": turn.get("p99_ms"),
                "mean_ms": turn.get("mean_ms"),
                "tps": v.get("throughput_turns_per_second"),
            }
        )
    return rows


def _render_markdown(manifest: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    out: list[str] = []
    out.append(f"# Benchmark sweep: {manifest.get('tag', 'headline')}")
    out.append("")
    out.append(
        f"- **topology**: `{manifest.get('topology')}`  "
        f"- **pass**: `{manifest.get('pass')}`  "
        f"- **started**: {manifest.get('started_utc')}  "
        f"- **ended**: {manifest.get('ended_utc')}"
    )
    git = manifest.get("git", {})
    out.append(f"- **commit**: `{git.get('sha', '?')[:12]}` on `{git.get('branch', '?')}`")
    out.append(
        f"- **variants**: concurrency={manifest.get('variants', {}).get('concurrency')}, "
        f"depths={manifest.get('variants', {}).get('history_depths')}, "
        f"iterations={manifest.get('variants', {}).get('iterations')}"
    )

    failed = manifest.get("backends_failed") or []
    if failed:
        out.append(f"- **failed backends**: `{', '.join(failed)}`")
    out.append("")

    # Sort rows: by depth asc, concurrency asc, then backend.
    rows_sorted = sorted(
        rows, key=lambda r: (r["depth"] or 0, r["concurrency"] or 0, r["backend"])
    )

    out.append("## Turn latency (ms) — lower is better")
    out.append("")
    out.append("| depth | concurrency | backend | p50 | p95 | p99 | mean | turns/s |")
    out.append("|------:|------------:|:--------|----:|----:|----:|-----:|--------:|")
    for r in rows_sorted:
        tps = r["tps"]
        tps_str = f"{tps:,.0f}" if isinstance(tps, (int, float)) else "—"
        out.append(
            f"| {r['depth']} | {r['concurrency']} | `{r['backend']}` | "
            f"{_fmt_ms(r['p50_ms'])} | {_fmt_ms(r['p95_ms'])} | "
            f"{_fmt_ms(r['p99_ms'])} | {_fmt_ms(r['mean_ms'])} | {tps_str} |"
        )
    out.append("")

    # Per-variant pivot: winner by p50 at each (depth, concurrency).
    out.append("## Winners by p50 at each (depth, concurrency)")
    out.append("")
    out.append("| depth | concurrency | winner | p50 ms | runner-up | p50 ms |")
    out.append("|------:|------------:|:-------|-------:|:----------|-------:|")
    buckets: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    for r in rows:
        v = r["p50_ms"]
        if v is None or (isinstance(v, float) and v != v):
            continue
        buckets.setdefault((r["depth"], r["concurrency"]), []).append(r)
    for (depth, conc), group in sorted(buckets.items(), key=lambda kv: (kv[0][0] or 0, kv[0][1] or 0)):
        ranked = sorted(group, key=lambda r: r["p50_ms"])
        winner = ranked[0]
        runner = ranked[1] if len(ranked) > 1 else None
        out.append(
            f"| {depth} | {conc} | `{winner['backend']}` | "
            f"{_fmt_ms(winner['p50_ms'])} | "
            f"{'`' + runner['backend'] + '`' if runner else '—'} | "
            f"{_fmt_ms(runner['p50_ms']) if runner else '—'} |"
        )
    out.append("")

    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "results_dir",
        type=Path,
        help="Path to a sweep results directory (created by run_sweep.sh).",
    )
    parser.add_argument(
        "--stdout-only",
        action="store_true",
        help="Print the summary but don't write SUMMARY.md.",
    )
    args = parser.parse_args()

    results_dir = args.results_dir
    if not results_dir.is_dir():
        sys.exit(f"not a directory: {results_dir}")

    manifest = _load_manifest(results_dir)
    backends = _load_backend_results(results_dir)

    rows: list[dict[str, Any]] = []
    for backend_name, blob in backends.items():
        rows.extend(_extract_rows(backend_name, blob))

    if not rows:
        sys.exit("no variants found in any result file; nothing to summarize")

    md = _render_markdown(manifest, rows)

    print(md)

    if not args.stdout_only:
        summary_path = results_dir / "SUMMARY.md"
        summary_path.write_text(md)
        print(f"\n[wrote {summary_path}]", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
