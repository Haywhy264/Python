#!/usr/bin/env python3
"""
Modbus TCP meter simulator for SiteSee2 automated testing (Version 3).

Version 3 additions
-------------------
- Defaults to host 0.0.0.0 so external Modbus TCP clients can connect
- Tracks currently connected client IP addresses
- Emits structured client-IP lines for GUI consumption:
    CLIENT_IPS=ip1,ip2

Register map (holding registers FC 03; mirrored to input registers FC 04):

  Addr  Words  Type     Unit  Description
  ----  -----  -------  ----  ------------------------------------------------
     0      2  float32  kW    Active power - 3-phase sum  (range 10..90 kW)
     2      2  float32  Hz    Mains frequency              (range 49.9..50.1 Hz)
     4      2  float32  V     Phase-A voltage, L-N RMS     (range 225..235 V)
     6      2  float32  A     Phase-A RMS current          (derived, always > 0)
     8      1  uint16   -     Relay state (1 = closed, 0 = open)
     9      1  uint16   -     Device status (0 = healthy)

Float32 values use big-endian word order (high word at the lower address).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import signal
import struct
import sys
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from pymodbus import ModbusDeviceIdentification
from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server.requesthandler import ServerRequestHandler
from pymodbus.server.server import ModbusTcpServer

logger = logging.getLogger(__name__)

CLIENT_IPS_TOKEN = "CLIENT_IPS="

# ---------------------------------------------------------------------------
# Register map constants
# ---------------------------------------------------------------------------

REGISTER_MAP: Dict[str, Dict] = {
    "active_power": {
        "address":     0,
        "count":       2,
        "type":        "float32",
        "unit":        "kW",
        "description": "Total active power (3-phase sum)",
    },
    "frequency": {
        "address":     2,
        "count":       2,
        "type":        "float32",
        "unit":        "Hz",
        "description": "Mains frequency",
    },
    "voltage": {
        "address":     4,
        "count":       2,
        "type":        "float32",
        "unit":        "V",
        "description": "Phase-A RMS voltage, phase-to-neutral",
    },
    "current": {
        "address":     6,
        "count":       2,
        "type":        "float32",
        "unit":        "A",
        "description": "Phase-A RMS current",
    },
    "relay_state": {
        "address":     8,
        "count":       1,
        "type":        "uint16",
        "unit":        "-",
        "description": "Relay state: 1 = closed, 0 = open",
    },
    "device_status": {
        "address":     9,
        "count":       1,
        "type":        "uint16",
        "unit":        "-",
        "description": "Device status: 0 = healthy",
    },
}

TOTAL_REGISTERS: int = 10
CYCLE_PERIOD_S: float = 60.0
_BLOCK_START: int = 1
_REGISTER_START: int = 0


@dataclass
class Stats:
    """Monotonically-increasing simulator counters."""

    request_count: int = 0
    connection_count: int = 0
    error_count: int = 0


class ValueGenerator:
    """Produces deterministic, repeatable simulated electrical-meter values."""

    def __init__(self, seed: int = 0) -> None:
        self._phase_offset: float = (seed % 360) * (math.pi / 180.0)

    def compute_values(self, t: float) -> Dict[str, float]:
        phase = (t / CYCLE_PERIOD_S) * 2.0 * math.pi + self._phase_offset

        active_power: float = 50.0 + 40.0 * math.sin(phase)
        frequency: float = 50.0 + 0.1 * math.sin(phase * 3)
        voltage: float = 230.0 + 5.0 * math.sin(phase * 2)
        current: float = (active_power * 1_000.0) / (3.0 * voltage * 0.95)

        return {
            "active_power": active_power,
            "frequency": frequency,
            "voltage": voltage,
            "current": current,
            "relay_state": 1.0,
            "device_status": 0.0,
        }

    @staticmethod
    def float_to_registers(value: float) -> Tuple[int, int]:
        packed = struct.pack(">f", value)
        high, low = struct.unpack(">HH", packed)
        return high, low

    @staticmethod
    def registers_to_float(high: int, low: int) -> float:
        packed = struct.pack(">HH", high, low)
        return struct.unpack(">f", packed)[0]


class MeterDataBlock(ModbusSequentialDataBlock):
    """Holding-register block with DEBUG-level logging on client access."""

    def __init__(self, address: int, values: List[int], stats: Stats) -> None:
        super().__init__(address, values)
        self._base_address = address
        self._stats = stats

    def getValues(self, address: int, count: int = 1) -> List[int]:  # noqa: N802
        parent_get = getattr(super(), "getValues", None)
        if callable(parent_get):
            result = parent_get(address, count)
        else:
            start = address - self._base_address
            end = start + count
            result = list(self.simdata[0].values[start:end])
        logger.debug("HR read   block_addr=%d  count=%d  values=%s", address, count, result)
        return result

    def setValues(self, address: int, values: List[int]) -> None:  # noqa: N802
        parent_set = getattr(super(), "setValues", None)
        if callable(parent_set):
            parent_set(address, values)
        else:
            if isinstance(values, int):
                values = [values]
            start = address - self._base_address
            end = start + len(values)
            backing = self.simdata[0].values
            if end > len(backing):
                backing.extend([0] * (end - len(backing)))
            backing[start:end] = list(values)
        logger.debug("HR write  block_addr=%d  values=%s", address, values)

    def update_internal(self, address: int, values: List[int]) -> None:
        self.setValues(address, values)


class IPTrackingServerRequestHandler(ServerRequestHandler):
    """Request handler that exposes peer IP changes to the owning server."""

    def _peer_ip(self) -> str:
        transport = getattr(self, "transport", None)
        if transport is None:
            return "unknown"
        peer = transport.get_extra_info("peername")
        if isinstance(peer, tuple) and peer:
            return str(peer[0])
        if peer:
            return str(peer)
        return "unknown"

    def callback_connected(self) -> None:
        super().callback_connected()
        self.server.notify_connection_event(self.unique_id, True, self._peer_ip())

    def callback_disconnected(self, exc: Exception | None) -> None:
        peer_ip = self._peer_ip()
        super().callback_disconnected(exc)
        self.server.notify_connection_event(self.unique_id, False, peer_ip)


class IPTrackingModbusTcpServer(ModbusTcpServer):
    """Modbus TCP server with per-connection peer-IP tracking."""

    def __init__(
        self,
        *args,
        on_client_event: Callable[[str, bool], None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._on_client_event = on_client_event
        self._connection_ip_by_id: dict[object, str] = {}

    def callback_new_connection(self) -> IPTrackingServerRequestHandler:
        return IPTrackingServerRequestHandler(
            self,
            self.trace_packet,
            self.trace_pdu,
            self.trace_connect,
        )

    def notify_connection_event(self, connection_id: object, connected: bool, peer_ip: str) -> None:
        ip = peer_ip
        if connected:
            self._connection_ip_by_id[connection_id] = ip
        else:
            ip = self._connection_ip_by_id.pop(connection_id, peer_ip)

        if self._on_client_event is not None:
            self._on_client_event(ip, connected)


class MeterSimulator:
    """Single-device Modbus TCP electrical-meter simulator (Version 3)."""

    DEFAULT_HOST: str = "0.0.0.0"
    DEFAULT_PORT: int = 502
    DEFAULT_UNIT_ID: int = 1
    DEFAULT_SEED: int = 0
    DEFAULT_UPDATE_INTERVAL_MS: int = 100
    DEFAULT_DISPLAY_INTERVAL_S: float = 1.0

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        unit_id: int = DEFAULT_UNIT_ID,
        seed: int = DEFAULT_SEED,
        update_interval_ms: int = DEFAULT_UPDATE_INTERVAL_MS,
        show_values: bool = True,
        display_interval_s: float = DEFAULT_DISPLAY_INTERVAL_S,
    ) -> None:
        self.host: str = host
        self.port: int = port
        self.unit_id: int = unit_id
        self.stats: Stats = Stats()
        self._generator: ValueGenerator = ValueGenerator(seed)
        self._update_interval: float = update_interval_ms / 1_000.0
        self._seed: int = seed
        self._show_values: bool = show_values
        self._display_interval: float = max(0.1, display_interval_s)
        self._last_display_ts: float = 0.0
        self._datablock: Optional[MeterDataBlock] = None
        self._ir_block: Optional[ModbusSequentialDataBlock] = None
        self._update_task: Optional[asyncio.Task] = None
        self._server: Optional[IPTrackingModbusTcpServer] = None
        self._running: bool = False
        self._ip_connection_counts: dict[str, int] = {}

    async def _on_runtime_access(
        self,
        func_code: int,
        _start_address: int,
        _address: int,
        _count: int,
        current_registers: list[int],
        set_values: list[int] | list[bool] | None,
    ) -> None:
        # Refresh read blocks just before response generation so clients always
        # get values derived from the current time.
        if set_values is not None:
            return
        if func_code not in (3, 4):
            return

        registers = self._pack_registers(self._generator.compute_values(time.time()))
        current_registers[:TOTAL_REGISTERS] = registers

    async def start(self) -> None:
        self._running = True

        initial = self._pack_registers(self._generator.compute_values(time.time()))
        self._datablock = MeterDataBlock(_BLOCK_START, initial, self.stats)
        self._ir_block = ModbusSequentialDataBlock(_BLOCK_START, list(initial))

        device_ctx = ModbusDeviceContext(
            hr=self._datablock,
            ir=self._ir_block,
        )
        device_ctx.simdevice.action = self._on_runtime_access
        logger.info("Runtime action enabled: %s", bool(device_ctx.simdevice.action))
        server_ctx = ModbusServerContext(
            devices={self.unit_id: device_ctx},
            single=False,
        )

        identity = ModbusDeviceIdentification()
        identity.VendorName = "SiteSee2 Meter Simulator V3"
        identity.ProductCode = "SIM-METER-3"
        identity.VendorUrl = "http://sitesee2.local"
        identity.ProductName = "Single Meter Simulator V3"
        identity.ModelName = "SIM-METER-3"
        identity.MajorMinorRevision = "3.0.0"

        self._update_task = asyncio.create_task(self._update_loop(), name="meter-update")

        self._server = IPTrackingModbusTcpServer(
            context=server_ctx,
            identity=identity,
            address=(self.host, self.port),
            trace_connect=self._trace_connect,
            trace_pdu=self._trace_pdu,
            on_client_event=self._on_client_event,
        )

        logger.info(
            "Meter simulator V3 listening on %s:%d  unit_id=%d  update_interval=%.0f ms  seed=%d",
            self.host,
            self.port,
            self.unit_id,
            self._update_interval * 1_000,
            self._seed,
        )
        self._emit_connected_ips()

        await self._server.serve_forever()

    async def stop(self) -> None:
        self._running = False

        if self._update_task is not None and not self._update_task.done():
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass

        if self._server is not None:
            try:
                await self._server.shutdown()
            except Exception:
                logger.debug("Server shutdown raised", exc_info=True)
            self._server = None

        logger.info(
            "Simulator stopped - requests=%d  connections=%d  errors=%d",
            self.stats.request_count,
            self.stats.connection_count,
            self.stats.error_count,
        )

    def _trace_connect(self, connected: bool) -> None:
        if connected:
            self.stats.connection_count += 1
            logger.info("Client connected event (total connections=%d)", self.stats.connection_count)
        else:
            logger.info("Client disconnected event")

    def _trace_pdu(self, sending: bool, pdu: object) -> object:
        if not sending:
            self.stats.request_count += 1
            fc = getattr(pdu, "function_code", None)
            logger.debug("Request PDU  fc=0x%02X", fc if fc is not None else 0)
        return pdu

    def _on_client_event(self, ip: str, connected: bool) -> None:
        if connected:
            self._ip_connection_counts[ip] = self._ip_connection_counts.get(ip, 0) + 1
            logger.info("Client connected from %s", ip)
        else:
            current = self._ip_connection_counts.get(ip, 0)
            if current <= 1:
                self._ip_connection_counts.pop(ip, None)
            else:
                self._ip_connection_counts[ip] = current - 1
            logger.info("Client disconnected from %s", ip)

        self._emit_connected_ips()

    def _emit_connected_ips(self) -> None:
        active_ips = sorted(self._ip_connection_counts.keys())
        rendered = ",".join(active_ips) if active_ips else "none"
        logger.info("%s%s", CLIENT_IPS_TOKEN, rendered)

    def _pack_registers(self, values: Dict[str, float]) -> List[int]:
        regs: List[int] = [0] * TOTAL_REGISTERS
        for name, meta in REGISTER_MAP.items():
            addr = meta["address"]
            if meta["type"] == "float32":
                hi, lo = ValueGenerator.float_to_registers(values[name])
                regs[addr] = hi
                regs[addr + 1] = lo
            else:
                regs[addr] = int(values[name])
        return regs

    def _format_values_line(self, values: Dict[str, float]) -> str:
        parts: List[str] = []
        for name, meta in REGISTER_MAP.items():
            value = values[name]
            if meta["type"] == "float32":
                rendered = f"{value:.3f}"
            else:
                rendered = str(int(value))
            unit = meta["unit"]
            if unit and unit != "-":
                rendered = f"{rendered} {unit}"
            parts.append(f"{name}={rendered}")
        return " | ".join(parts)

    async def _update_loop(self) -> None:
        while self._running:
            try:
                values = self._generator.compute_values(time.time())
                if self._show_values:
                    now_monotonic = time.monotonic()
                    if now_monotonic - self._last_display_ts >= self._display_interval:
                        logger.info("Simulated values: %s", self._format_values_line(values))
                        self._last_display_ts = now_monotonic
            except Exception:
                self.stats.error_count += 1
                logger.exception("Error in update loop")
            await asyncio.sleep(self._update_interval)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Modbus TCP meter simulator V3 for SiteSee2 testing",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--host",
        default=MeterSimulator.DEFAULT_HOST,
        metavar="ADDR",
        help="Bind address (0.0.0.0 listens on all interfaces for external clients)",
    )
    p.add_argument(
        "--port",
        default=MeterSimulator.DEFAULT_PORT,
        type=int,
        metavar="N",
        help="TCP port (ports <= 1023 require elevated privileges)",
    )
    p.add_argument(
        "--unit-id",
        default=MeterSimulator.DEFAULT_UNIT_ID,
        type=int,
        dest="unit_id",
        metavar="N",
        help="Modbus unit / slave ID (1-247)",
    )
    p.add_argument(
        "--seed",
        default=MeterSimulator.DEFAULT_SEED,
        type=int,
        metavar="N",
        help="Waveform phase seed (0-359); same seed -> same waveform on restart",
    )
    p.add_argument(
        "--interval",
        default=MeterSimulator.DEFAULT_UPDATE_INTERVAL_MS,
        type=int,
        dest="interval",
        metavar="MS",
        help="Register update interval in milliseconds",
    )
    p.add_argument(
        "--hide-values",
        action="store_true",
        help="Disable periodic logging of simulated parameter values",
    )
    p.add_argument(
        "--display-interval",
        default=MeterSimulator.DEFAULT_DISPLAY_INTERVAL_S,
        type=float,
        metavar="SEC",
        help="How often to print simulated values in seconds",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging",
    )
    return p


async def _async_main(args: argparse.Namespace) -> None:
    _configure_logging(args.verbose)
    simulator = MeterSimulator(
        host=args.host,
        port=args.port,
        unit_id=args.unit_id,
        seed=args.seed,
        update_interval_ms=args.interval,
        show_values=not args.hide_values,
        display_interval_s=args.display_interval,
    )

    loop = asyncio.get_running_loop()

    def _shutdown() -> None:
        logger.info("Shutdown signal received - stopping ...")
        asyncio.ensure_future(simulator.stop())

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _shutdown)
    else:
        signal.signal(signal.SIGTERM, lambda *_: loop.call_soon_threadsafe(_shutdown))

    await simulator.start()


def main() -> None:
    args = build_arg_parser().parse_args()
    try:
        asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
