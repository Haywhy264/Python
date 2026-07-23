#!/usr/bin/env python3
"""
Modbus TCP Load Tester
======================
Spawns NUM_CLIENTS concurrent async Modbus TCP connections, each issuing
POLLS_EACH read-holding-register requests, then prints a latency/throughput
summary.

Usage
-----
Edit TARGET_IP / TARGET_PORT / NUM_CLIENTS / POLLS_EACH below, then run:
    python load_test_modbus.py

Increase NUM_CLIENTS incrementally (10 → 50 → 100) to find the point where
the target application starts returning errors or latency degrades.
"""
from __future__ import annotations

import asyncio
import socket
import statistics
import time

from pymodbus.client import AsyncModbusTcpClient

# ---------------------------------------------------------------------------
# TCP keep-alive settings
# ---------------------------------------------------------------------------
_KA_IDLE_S    = 10   # seconds before the first keepalive probe (Linux / Windows)
_KA_INTVL_S   = 5    # seconds between subsequent probes    (Linux)
_KA_CNT       = 3    # number of unanswered probes before drop (Linux)


def _apply_keepalive(sock: socket.socket) -> None:
    """Enable TCP keepalive on *sock* using the best options available."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

        # Linux — fine-grained per-socket knobs
        if hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE,  _KA_IDLE_S)
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, _KA_INTVL_S)
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT,   _KA_CNT)

        # Windows — one ioctl call: (enable, idle_ms, interval_ms)
        if hasattr(socket, "SIO_KEEPALIVE_VALS"):
            sock.ioctl(
                socket.SIO_KEEPALIVE_VALS,
                (1, _KA_IDLE_S * 1000, _KA_INTVL_S * 1000),
            )
    except OSError:
        pass  # keepalive is best-effort; never block the test

# ---------------------------------------------------------------------------
# Configuration — edit these before running
# ---------------------------------------------------------------------------
TARGET_IP   = "192.168.1.2"   # IP of the application under test
TARGET_PORT = 502              # Modbus TCP port (default 502; simulator default 5021)
UNIT_ID     = 1                # Modbus unit / slave ID
NUM_CLIENTS = 10               # number of concurrent TCP connections
POLLS_EACH  = 20               # how many read requests each client sends
REGISTER    = 0                # starting holding-register address
REG_COUNT   = 2                # number of registers to read per request
# ---------------------------------------------------------------------------

latencies: list[float] = []
error_count = 0
_lock = asyncio.Lock()


async def client_task(client_id: int) -> None:
    """Connect, apply TCP keepalive, poll POLLS_EACH times, then close."""
    global error_count

    client = AsyncModbusTcpClient(
        TARGET_IP,
        port=TARGET_PORT,
        reconnect_delay=0.5,      # seconds before first auto-reconnect
        reconnect_delay_max=5.0,  # cap for exponential back-off
        timeout=10,
    )
    await client.connect()

    # Apply TCP keepalive so idle connections survive NAT/firewall timeouts
    sock = client.transport.get_extra_info("socket") if client.transport else None
    if sock is not None:
        _apply_keepalive(sock)

    try:
        for poll in range(POLLS_EACH):
            t0 = time.perf_counter()
            result = await client.read_holding_registers(
                address=REGISTER,
                count=REG_COUNT,
                device_id=UNIT_ID,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0

            async with _lock:
                if result.isError():
                    error_count += 1
                    print(f"  [client {client_id:>3}] poll {poll + 1}/{POLLS_EACH} ERROR: {result}")
                else:
                    latencies.append(elapsed_ms)
    finally:
        client.close()


def print_report(total_requests: int, total_time: float) -> None:
    """Print a formatted summary to stdout."""
    successful = len(latencies)
    print()
    print("=" * 52)
    print("  LOAD TEST RESULTS")
    print("=" * 52)
    print(f"  Target          : {TARGET_IP}:{TARGET_PORT}  (unit {UNIT_ID})")
    print(f"  Concurrent clients : {NUM_CLIENTS}")
    print(f"  Polls per client   : {POLLS_EACH}")
    print(f"  Total requests     : {total_requests}")
    print(f"  Successful         : {successful}")
    print(f"  Errors             : {error_count}")
    print(f"  Total time         : {total_time:.2f} s")
    print(f"  Throughput         : {total_requests / total_time:.1f} req/s")
    if latencies:
        sorted_lat = sorted(latencies)
        p95_index  = max(0, int(len(sorted_lat) * 0.95) - 1)
        p99_index  = max(0, int(len(sorted_lat) * 0.99) - 1)
        print(f"  Latency  min       : {min(latencies):.1f} ms")
        print(f"  Latency  avg       : {statistics.mean(latencies):.1f} ms")
        print(f"  Latency  median    : {statistics.median(latencies):.1f} ms")
        print(f"  Latency  p95       : {sorted_lat[p95_index]:.1f} ms")
        print(f"  Latency  p99       : {sorted_lat[p99_index]:.1f} ms")
        print(f"  Latency  max       : {max(latencies):.1f} ms")
    print("=" * 52)


async def main() -> None:
    total_requests = NUM_CLIENTS * POLLS_EACH
    print(f"Starting load test — {NUM_CLIENTS} clients × {POLLS_EACH} polls "
          f"= {total_requests} total requests")
    print(f"Target: {TARGET_IP}:{TARGET_PORT}  register {REGISTER} "
          f"(count={REG_COUNT}, unit={UNIT_ID})\n")

    t_start = time.perf_counter()
    await asyncio.gather(*[client_task(i) for i in range(NUM_CLIENTS)])
    total_time = time.perf_counter() - t_start

    print_report(total_requests, total_time)


if __name__ == "__main__":
    asyncio.run(main())
