"""Repeatable query-plan / index-coverage audit (C3).

Run against any environment with ``DATABASE_URL`` set + ``pg_stat_statements``
enabled (it is on the Supabase prod DB):

    uv run python scripts/query_plan_audit.py

It surfaces three things:
  1. The most-frequently-executed SELECTs (the real hot paths).
  2. The slowest-by-mean queries (latency outliers).
  3. Per-table seq-scan vs index-scan ratios — flagging only tables that are
     BOTH sizeable (> ``MIN_ROWS``) AND seq-scan-heavy, since a high seq ratio
     on a tiny table is the planner correctly choosing a scan over an index
     (NOT a missing index). Those flagged rows are the genuine
     missing-index candidates.

This is a read-only diagnostic — it never mutates. Re-run it as the data set
grows; a table that's tiny today may cross ``MIN_ROWS`` later and start
warranting an index.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.db.session import get_engine

# A table smaller than this is fine to seq-scan regardless of ratio — the
# planner's choice is correct and an index would be dead weight.
MIN_ROWS = 2000


async def main() -> None:
    engine = get_engine()
    async with engine.begin() as c:
        print("=== most-called SELECTs (production hot paths) ===")
        rows = await c.execute(
            text(
                """
                SELECT calls, round(mean_exec_time::numeric, 3) AS mean_ms,
                       left(query, 90) AS q
                FROM pg_stat_statements
                WHERE query ILIKE 'SELECT%FROM %'
                  AND query NOT ILIKE '%pg_%'
                  AND query NOT ILIKE '%information_schema%'
                ORDER BY calls DESC LIMIT 15
                """
            )
        )
        for r in rows:
            print(f"  x{r.calls:<8} {r.mean_ms:>7} ms  {r.q}")

        print("\n=== slowest by mean (latency outliers) ===")
        rows = await c.execute(
            text(
                """
                SELECT round(mean_exec_time::numeric, 2) AS mean_ms, calls,
                       left(query, 90) AS q
                FROM pg_stat_statements
                WHERE query ILIKE '%FROM %'
                  AND query NOT ILIKE '%pg_%'
                  AND query NOT ILIKE '%information_schema%'
                ORDER BY mean_exec_time DESC LIMIT 10
                """
            )
        )
        for r in rows:
            print(f"  {r.mean_ms:>8} ms  x{r.calls:<6} {r.q}")

        print(
            f"\n=== missing-index candidates "
            f"(seq-heavy AND > {MIN_ROWS} rows) ==="
        )
        rows = await c.execute(
            text(
                """
                SELECT relname, seq_scan, idx_scan, n_live_tup
                FROM pg_stat_user_tables
                WHERE schemaname = 'public' AND seq_scan > 0
                ORDER BY seq_scan DESC
                """
            )
        )
        flagged = False
        for r in rows:
            total = r.seq_scan + (r.idx_scan or 0)
            ratio = r.seq_scan / total if total else 0.0
            if r.n_live_tup > MIN_ROWS and ratio > 0.5:
                flagged = True
                print(
                    f"  ⚠ {r.relname}: seq={r.seq_scan} idx={r.idx_scan or 0} "
                    f"rows={r.n_live_tup} seq_ratio={ratio:.2f}"
                )
        if not flagged:
            print(
                "  none — every seq-heavy table is small enough that a scan is "
                "the planner's correct choice. Schema is well-indexed for the "
                "current access patterns."
            )


if __name__ == "__main__":
    asyncio.run(main())
