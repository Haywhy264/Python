#!/usr/bin/env python3
"""
Modbus TCP meter simulator for SiteSee2 automated testing.

Register map (holding registers FC 03; mirrored to input registers FC 04):

  Addr  Words  Type     Unit  Description
  ----  -----  -------  ----  ------------------------------------------------
     0      2  float32  kW    Active power – 3-phase sum  (range 10..90 kW)
     2      2  float32  Hz    Mains frequency              (range 49.9..50.1 Hz)
     4      2  float32  V     Phase-A voltage, L-N RMS     (range 225..235 V)
     6      2  float32  A     Phase-A RMS current          (derived, always > 0)
     8      1  uint16   –     Relay state (1 = closed, 0 = open)
     9      1  uint16   –     Device status (0 = healthy)

Float32 values use **big-endian word order** (high word at the lower address).

Usage
-----
    python modbus_meter_simulator.py [--host ADDR] [--port N] [--unit-id N]
                                     [--seed N] [--interval MS] [-v]

Requires pymodbus >= 3.7.
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
from typing import Dict, List, Optional, Tuple

from pymodbus import ModbusDeviceIdentification
from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import StartAsyncTcpServer, ServerAsyncStop

logger = logging.getLogger(__name__)

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

# Total number of 16-bit register words in the map.
TOTAL_REGISTERS: int = 10

# Seconds per full waveform cycle.
CYCLE_PERIOD_S: float = 60.0

# pymodbus ModbusDeviceContext.getValues/setValues adds 1 to PDU address before
# accessing the data block, so the block must start at address 1 to make PDU
# address 0 correspond to block index 0.
_BLOCK_START: int = 1


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@dataclass
class Stats:
    """Monotonically-increasing simulator counters."""
    request_count:    int = 0
    connection_count: int = 0
    error_count:      int = 0


# ---------------------------------------------------------------------------
# Value generator
# ---------------------------------------------------------------------------

class ValueGenerator:
    """
    Produces deterministic, repeatable simulated electrical-meter values.

    Values follow smooth sinusoidal waveforms keyed to wall-clock time.
    The *seed* parameter controls the waveform phase offset so that the same
    seed always produces the same value sequence across restarts.

    Healthy-device waveform ranges
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    active_power  : 10 .. 90 kW         (60-second cycle)
    frequency     : 49.9 .. 50.1 Hz
    voltage       : 225 .. 235 V
    current       : derived (P = 3 · V · I · PF, PF = 0.95)
    relay_state   : 1 (always closed)
    device_status : 0 (always healthy)
    """

    def __init__(self, seed: int = 0) -> None:
        # Map seed to a phase offset; each degree of seed = π/180 rad.
        self._phase_offset: float = (seed % 360) * (math.pi / 180.0)

    def compute_values(self, t: float) -> Dict[str, float]:
        """Return simulated meter values at epoch-seconds *t*."""
        phase = (t / CYCLE_PERIOD_S) * 2.0 * math.pi + self._phase_offset

        active_power: float = 50.0 + 40.0 * math.sin(phase)      # 10..90 kW  (period = 60 s)
        frequency:    float = 50.0 +  0.1 * math.sin(phase * 3)  # 49.9..50.1 Hz (period = 20 s)
        voltage:      float = 230.0 +  5.0 * math.sin(phase * 2)  # 225..235 V   (period = 30 s)
        # 3-phase: P = 3 × V_LN × I × PF  →  I = P / (3 × V × PF)
        current:      float = (active_power * 1_000.0) / (3.0 * voltage * 0.95)

        return {
            "active_power":  active_power,
            "frequency":     frequency,
            "voltage":       voltage,
            "current":       current,
            "relay_state":   1.0,
            "device_status": 0.0,
        }

    @staticmethod
    def float_to_registers(value: float) -> Tuple[int, int]:
        """Encode *value* as IEEE-754 float32 → ``(high_word, low_word)``."""
        packed    = struct.pack(">f", value)
        high, low = struct.unpack(">HH", packed)
        return high, low

    @staticmethod
    def registers_to_float(high: int, low: int) -> float:
        """Decode ``(high_word, low_word)`` from big-endian float32."""
        packed = struct.pack(">HH", high, low)
        return struct.unpack(">f", packed)[0]


# ---------------------------------------------------------------------------
# Data block
# ---------------------------------------------------------------------------

class MeterDataBlock(ModbusSequentialDataBlock):
    """
    Holding-register block with DEBUG-level logging on every client access.

    Call :meth:`update_internal` for simulator-driven writes so they are
    silent and do not interfere with request statistics.
    """

    def __init__(self, address: int, values: List[int], stats: Stats) -> None:
        super().__init__(address, values)
        self._base_address = address
        self._stats = stats

    def getValues(self, address: int, count: int = 1) -> List[int]:  # noqa: N802
        # pymodbus<=3.13 exposed getValues/setValues on the base class.
        # pymodbus>=3.14 removed them and stores values in simdata.
        parent_get = getattr(super(), "getValues", None)
        if callable(parent_get):
            result = parent_get(address, count)
        else:
            start = address - self._base_address
            end = start + count
            result = list(self.simdata[0].values[start:end])
        logger.debug(
            "HR read   block_addr=%d  count=%d  values=%s",
            address, count, result,
        )
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
        """Bypass the Modbus interface; used exclusively by the update task."""
        self.setValues(address, values)


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------

class MeterSimulator:
    """Single-device Modbus TCP electrical-meter simulator."""

    DEFAULT_HOST:               str = "127.0.0.1"
    DEFAULT_PORT:               int = 502
    DEFAULT_UNIT_ID:            int = 1
    DEFAULT_SEED:               int = 0
    DEFAULT_UPDATE_INTERVAL_MS: int = 100
    DEFAULT_DISPLAY_INTERVAL_S: float = 1.0

    def __init__(
        self,
        host:               str = DEFAULT_HOST,
        port:               int = DEFAULT_PORT,
        unit_id:            int = DEFAULT_UNIT_ID,
        seed:               int = DEFAULT_SEED,
        update_interval_ms: int = DEFAULT_UPDATE_INTERVAL_MS,
        show_values:        bool = True,
        display_interval_s: float = DEFAULT_DISPLAY_INTERVAL_S,
    ) -> None:
        self.host:             str   = host
        self.port:             int   = port
        self.unit_id:          int   = unit_id
        self.stats:            Stats = Stats()
        self._generator:       ValueGenerator        = ValueGenerator(seed)
        self._update_interval: float                 = update_interval_ms / 1_000.0
        self._seed:            int                   = seed
        self._show_values:     bool                  = show_values
        self._display_interval: float                = max(0.1, display_interval_s)
        self._last_display_ts: float                 = 0.0
        self._datablock:       Optional[MeterDataBlock] = None
        self._update_task:     Optional[asyncio.Task]   = None
        self._running:         bool                  = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the Modbus TCP server.  Blocks until :meth:`stop` is called."""
        self._running = True

        initial         = self._pack_registers(self._generator.compute_values(time.time()))
        # Block starts at _BLOCK_START so that PDU address 0 maps to block index 0.
        # (ModbusDeviceContext.getValues adds 1 to every incoming PDU address.)
        self._datablock = MeterDataBlock(_BLOCK_START, initial, self.stats)
        ir_block        = ModbusSequentialDataBlock(_BLOCK_START, list(initial))

        device_ctx = ModbusDeviceContext(
            hr=self._datablock,
            ir=ir_block,
        )
        server_ctx = ModbusServerContext(
            devices={self.unit_id: device_ctx},
            single=False,
        )

        identity                    = ModbusDeviceIdentification()
        identity.VendorName         = "SiteSee2 Meter Simulator V2"
        identity.ProductCode        = "SIM-METER-2"
        identity.VendorUrl          = "http://sitesee2.local"
        identity.ProductName        = "Single Meter Simulator V2"
        identity.ModelName          = "SIM-METER-2"
        identity.MajorMinorRevision = "2.0.0"

        self._update_task = asyncio.create_task(
            self._update_loop(), name="meter-update"
        )

        logger.info(
            "Meter simulator listening on %s:%d  unit_id=%d  "
            "update_interval=%.0f ms  seed=%d",
            self.host, self.port, self.unit_id,
            self._update_interval * 1_000,
            self._seed,
        )

        await StartAsyncTcpServer(
            context=server_ctx,
            identity=identity,
            address=(self.host, self.port),
            trace_connect=self._trace_connect,
            trace_pdu=self._trace_pdu,
        )

    async def stop(self) -> None:
        """Gracefully shut down the update task and the TCP server."""
        self._running = False

        if self._update_task is not None and not self._update_task.done():
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass

        try:
            await ServerAsyncStop()
        except Exception:
            logger.debug(
                "ServerAsyncStop raised (expected if not running)", exc_info=True
            )

        logger.info(
            "Simulator stopped — requests=%d  connections=%d  errors=%d",
            self.stats.request_count,
            self.stats.connection_count,
            self.stats.error_count,
        )

    # ------------------------------------------------------------------
    # Callbacks (synchronous — called by the pymodbus server internals)
    # ------------------------------------------------------------------

    def _trace_connect(self, connected: bool) -> None:
        """Called by pymodbus when a client connects (True) or disconnects (False)."""
        if connected:
            self.stats.connection_count += 1
            logger.info(
                "Client connected     (total connections=%d)",
                self.stats.connection_count,
            )
        else:
            logger.info("Client disconnected")

    def _trace_pdu(self, sending: bool, pdu: object) -> object:
        """Called by pymodbus for every PDU received or sent.

        *sending=False* means the server received a request from the client.
        *sending=True*  means the server is about to send a response.
        The PDU must be returned unchanged.
        """
        if not sending:
            self.stats.request_count += 1
            fc = getattr(pdu, "function_code", None)
            logger.debug("Request PDU  fc=0x%02X", fc if fc is not None else 0)
        return pdu

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _pack_registers(self, values: Dict[str, float]) -> List[int]:
        """Convert a values dict into a flat list of ``TOTAL_REGISTERS`` uint16 words."""
        regs: List[int] = [0] * TOTAL_REGISTERS
        for name, meta in REGISTER_MAP.items():
            addr = meta["address"]
            if meta["type"] == "float32":
                hi, lo        = ValueGenerator.float_to_registers(values[name])
                regs[addr]    = hi
                regs[addr + 1] = lo
            else:
                regs[addr] = int(values[name])
        return regs

    def _format_values_line(self, values: Dict[str, float]) -> str:
        """Format simulated values for readable console output."""
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
        """Periodically refresh holding and input register values."""
        while self._running:
            try:
                values    = self._generator.compute_values(time.time())
                registers = self._pack_registers(values)
                if self._datablock is not None:
                    self._datablock.update_internal(_BLOCK_START, registers)
                if self._show_values:
                    now_monotonic = time.monotonic()
                    if now_monotonic - self._last_display_ts >= self._display_interval:
                        logger.info("Simulated values: %s", self._format_values_line(values))
                        self._last_display_ts = now_monotonic
            except Exception:
                self.stats.error_count += 1
                logger.exception("Error in update loop")
            await asyncio.sleep(self._update_interval)


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level   = logging.DEBUG if verbose else logging.INFO,
        format  = "%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
        datefmt = "%Y-%m-%dT%H:%M:%S",
    )


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Modbus TCP meter simulator for SiteSee2 testing",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--host",
        default=MeterSimulator.DEFAULT_HOST,
        metavar="ADDR",
        help="Bind address (0.0.0.0 listens on all interfaces)",
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
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging",
    )
    return p


async def _async_main(args: argparse.Namespace) -> None:
    _configure_logging(args.verbose)
    simulator = MeterSimulator(
        host               = args.host,
        port               = args.port,
        unit_id            = args.unit_id,
        seed               = args.seed,
        update_interval_ms = args.interval,
        show_values        = not args.hide_values,
        display_interval_s = args.display_interval,
    )

    loop = asyncio.get_running_loop()

    def _shutdown() -> None:
        logger.info("Shutdown signal received — stopping ...")
        asyncio.ensure_future(simulator.stop())

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _shutdown)
    else:
        # Windows: loop.add_signal_handler is not supported for all signals.
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
