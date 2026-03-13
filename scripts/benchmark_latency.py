#!/usr/bin/env python3
"""
Benchmark: ExecutionService latency (simulation mode).

Usage:
    python scripts/benchmark_latency.py
    python scripts/benchmark_latency.py --iterations 500
"""
import argparse
import asyncio
import statistics
import time
import sys
import os
from decimal import Decimal
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from polymarket_arb.services.execution import ExecutionService

FAKE_UP_TOKEN   = "71321045679252212594626385532706912750332728571942532289631379312455583992563"
FAKE_DOWN_TOKEN = "52114319501245915516055106046884209969926127482827954674443846427813813222791"
FAKE_SLUG       = "btc-updown-5m-benchmark"

async def run_benchmark(iterations: int) -> None:
    svc = ExecutionService(simulation=True)
    samples_ms: list[float] = []

    print(f"Running {iterations} simulated trades...")

    for _ in range(iterations):
        t0 = time.perf_counter()
        await svc.execute_neg_risk(
            market_slug=FAKE_SLUG,
            up_token_id=FAKE_UP_TOKEN,
            down_token_id=FAKE_DOWN_TOKEN,
            up_ask=Decimal("0.47"),
            down_ask=Decimal("0.47"),
            shares=Decimal("10"),
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        samples_ms.append(elapsed_ms)

    sorted_samples = sorted(samples_ms)
    n = len(sorted_samples)

    def percentile(data, p):
        idx = int(len(data) * p / 100)
        return data[min(idx, len(data) - 1)]

    print(f"\n{'='*50}")
    print(f"  ExecutionService Benchmark ({iterations} iterations)")
    print(f"{'='*50}")
    print(f"  Mode          : simulation")
    print(f"  Min           : {min(sorted_samples):.3f} ms")
    print(f"  Avg           : {statistics.mean(sorted_samples):.3f} ms")
    print(f"  P50 (median)  : {statistics.median(sorted_samples):.3f} ms")
    print(f"  P95           : {percentile(sorted_samples, 95):.3f} ms")
    print(f"  P99           : {percentile(sorted_samples, 99):.3f} ms")
    print(f"  Max           : {max(sorted_samples):.3f} ms")
    print(f"{'='*50}")
    print(f"\nThroughput (sim): ~{1000/statistics.mean(sorted_samples):.0f} trades/sec")
    print(f"Stats from service: {svc.stats}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark ExecutionService latency")
    parser.add_argument("--iterations", type=int, default=1000, help="Number of iterations")
    args = parser.parse_args()
    asyncio.run(run_benchmark(args.iterations))
