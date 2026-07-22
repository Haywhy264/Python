"""
Pytest suite for the Modbus TCP meter simulator.

Unit-test coverage
~~~~~~~~~~~~~~~~~~
- ValueGenerator  : value ranges, determinism, cross-seed uniqueness, periodicity
- Float encoding  : float_to_registers / registers_to_float round-trip
- Register map    : completeness, no gaps, no overlaps, known addresses
- MeterSimulator  : _pack_registers correctness, default configuration
- MeterDataBlock  : update_internal data integrity, getValues slicing
- Stats           : default values, increment behaviour
- _request_tracer : increments request_count per call

Integration test (requires a free localhost port; marked 'integration')
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
- Start a real server, read all holding registers via a Modbus client,
  verify decoded values are within expected ranges, then stop cleanly.
"""
from __future__ import annotations

import asyncio
import math
import struct

import pytest

from modbus_meter_simulator import (
    CYCLE_PERIOD_S,
    REGISTER_MAP,
    TOTAL_REGISTERS,
    MeterDataBlock,
    MeterSimulator,
    Stats,
    ValueGenerator,
)

# ---------------------------------------------------------------------------
# ValueGenerator – ranges
# ---------------------------------------------------------------------------

SAMPLE_TIMES = [float(t) for t in range(0, int(CYCLE_PERIOD_S), 3)]
GEN_SEED0 = ValueGenerator(seed=0)


class TestValueGeneratorRanges:
    """All values must remain inside documented healthy-device bounds."""

    def test_active_power_range(self):
        for t in SAMPLE_TIMES:
            v = GEN_SEED0.compute_values(t)["active_power"]
            assert 10.0 <= v <= 90.0, f"active_power={v!r} out of range at t={t}"

    def test_frequency_range(self):
        for t in SAMPLE_TIMES:
            v = GEN_SEED0.compute_values(t)["frequency"]
            assert 49.9 <= v <= 50.1, f"frequency={v!r} out of range at t={t}"

    def test_voltage_range(self):
        for t in SAMPLE_TIMES:
            v = GEN_SEED0.compute_values(t)["voltage"]
            assert 225.0 <= v <= 235.0, f"voltage={v!r} out of range at t={t}"

    def test_current_always_positive(self):
        for t in SAMPLE_TIMES:
            v = GEN_SEED0.compute_values(t)["current"]
            assert v > 0.0, f"current={v!r} must be positive at t={t}"

    def test_relay_state_always_closed(self):
        for t in SAMPLE_TIMES:
            assert GEN_SEED0.compute_values(t)["relay_state"] == 1.0

    def test_device_status_always_healthy(self):
        for t in SAMPLE_TIMES:
            assert GEN_SEED0.compute_values(t)["device_status"] == 0.0

    def test_all_keys_present(self):
        keys = set(GEN_SEED0.compute_values(0.0).keys())
        assert keys == set(REGISTER_MAP.keys())


# ---------------------------------------------------------------------------
# ValueGenerator – determinism and periodicity
# ---------------------------------------------------------------------------

class TestValueGeneratorDeterminism:
    def test_same_seed_same_time_produces_identical_values(self):
        g1 = ValueGenerator(seed=13)
        g2 = ValueGenerator(seed=13)
        t  = 99_999.0
        assert g1.compute_values(t) == g2.compute_values(t)

    def test_different_seeds_produce_different_active_power(self):
        t  = 12_345.0
        v0 = ValueGenerator(seed=0).compute_values(t)["active_power"]
        v1 = ValueGenerator(seed=90).compute_values(t)["active_power"]
        # Seeds 0 and 90 differ by 90° – sin(x) ≠ sin(x + π/2)
        assert not math.isclose(v0, v1, rel_tol=0.01), (
            "Seeds 0 and 90 should produce different active-power values"
        )

    def test_values_repeat_every_cycle(self):
        gen = ValueGenerator(seed=0)
        t   = 1_234.5
        v1  = gen.compute_values(t)
        v2  = gen.compute_values(t + CYCLE_PERIOD_S)
        for key in ("active_power", "frequency", "voltage", "current"):
            assert math.isclose(v1[key], v2[key], rel_tol=1e-6), (
                f"{key} did not repeat after one cycle"
            )

    def test_values_change_over_time(self):
        gen = ValueGenerator(seed=0)
        v0  = gen.compute_values(0.0)["active_power"]
        v15 = gen.compute_values(15.0)["active_power"]
        # Quarter-cycle apart → should differ noticeably
        assert not math.isclose(v0, v15, rel_tol=0.01)


# ---------------------------------------------------------------------------
# Float encoding round-trip
# ---------------------------------------------------------------------------

class TestFloatEncoding:
    CASES = [0.0, 1.0, -1.0, 50.0, 230.0, 0.001, 1_000.0, -999.9, 1e6]

    def test_roundtrip(self):
        for value in self.CASES:
            hi, lo = ValueGenerator.float_to_registers(value)
            back   = ValueGenerator.registers_to_float(hi, lo)
            assert math.isclose(value, back, rel_tol=1e-5, abs_tol=1e-30), (
                f"Round-trip failed for {value}: got {back}"
            )

    def test_output_is_valid_uint16_pair(self):
        for value in self.CASES:
            hi, lo = ValueGenerator.float_to_registers(value)
            assert 0 <= hi <= 0xFFFF, f"high word out of range: {hi}"
            assert 0 <= lo <= 0xFFFF, f"low word out of range: {lo}"

    def test_big_endian_word_order(self):
        """High word must match the first two bytes of the big-endian float32."""
        value    = 230.5
        packed   = struct.pack(">f", value)
        hi, _    = ValueGenerator.float_to_registers(value)
        expected = struct.unpack(">H", packed[:2])[0]
        assert hi == expected

    def test_low_word_matches_last_two_bytes(self):
        value  = 50.05
        packed = struct.pack(">f", value)
        _, lo  = ValueGenerator.float_to_registers(value)
        expected = struct.unpack(">H", packed[2:])[0]
        assert lo == expected


# ---------------------------------------------------------------------------
# Register map integrity
# ---------------------------------------------------------------------------

class TestRegisterMap:
    def test_total_word_count_equals_total_registers(self):
        total = sum(m["count"] for m in REGISTER_MAP.values())
        assert total == TOTAL_REGISTERS

    def test_no_address_gaps_or_overlaps(self):
        occupied: set = set()
        for name, meta in REGISTER_MAP.items():
            for addr in range(meta["address"], meta["address"] + meta["count"]):
                assert addr not in occupied, (
                    f"Address {addr} (from '{name}') is already occupied"
                )
                occupied.add(addr)
        assert occupied == set(range(TOTAL_REGISTERS)), (
            "Register map does not cover all addresses 0..TOTAL_REGISTERS-1"
        )

    def test_known_register_addresses(self):
        assert REGISTER_MAP["active_power"]["address"]  == 0
        assert REGISTER_MAP["frequency"]["address"]     == 2
        assert REGISTER_MAP["voltage"]["address"]       == 4
        assert REGISTER_MAP["current"]["address"]       == 6
        assert REGISTER_MAP["relay_state"]["address"]   == 8
        assert REGISTER_MAP["device_status"]["address"] == 9

    def test_float32_fields_have_count_two(self):
        for name, meta in REGISTER_MAP.items():
            if meta["type"] == "float32":
                assert meta["count"] == 2, (
                    f"float32 field '{name}' should have count=2"
                )

    def test_uint16_fields_have_count_one(self):
        for name, meta in REGISTER_MAP.items():
            if meta["type"] == "uint16":
                assert meta["count"] == 1, (
                    f"uint16 field '{name}' should have count=1"
                )


# ---------------------------------------------------------------------------
# MeterSimulator._pack_registers
# ---------------------------------------------------------------------------

class TestPackRegisters:
    SIM    = MeterSimulator()
    GEN    = ValueGenerator(seed=0)
    VALUES = GEN.compute_values(0.0)
    REGS   = SIM._pack_registers(VALUES)

    def test_output_length(self):
        assert len(self.REGS) == TOTAL_REGISTERS

    def test_active_power_decodes_correctly(self):
        hi, lo  = self.REGS[0], self.REGS[1]
        decoded = ValueGenerator.registers_to_float(hi, lo)
        assert math.isclose(decoded, self.VALUES["active_power"], rel_tol=1e-5)

    def test_frequency_decodes_correctly(self):
        hi, lo  = self.REGS[2], self.REGS[3]
        decoded = ValueGenerator.registers_to_float(hi, lo)
        assert math.isclose(decoded, self.VALUES["frequency"], rel_tol=1e-5)

    def test_voltage_decodes_correctly(self):
        hi, lo  = self.REGS[4], self.REGS[5]
        decoded = ValueGenerator.registers_to_float(hi, lo)
        assert math.isclose(decoded, self.VALUES["voltage"], rel_tol=1e-5)

    def test_current_decodes_correctly(self):
        hi, lo  = self.REGS[6], self.REGS[7]
        decoded = ValueGenerator.registers_to_float(hi, lo)
        assert math.isclose(decoded, self.VALUES["current"], rel_tol=1e-5)

    def test_relay_state_is_one(self):
        assert self.REGS[8] == 1

    def test_device_status_is_zero(self):
        assert self.REGS[9] == 0

    def test_all_words_are_valid_uint16(self):
        for i, word in enumerate(self.REGS):
            assert 0 <= word <= 0xFFFF, f"Register {i} value {word} is not uint16"


# ---------------------------------------------------------------------------
# MeterDataBlock
# ---------------------------------------------------------------------------

class TestMeterDataBlock:
    def _make_block(self) -> MeterDataBlock:
        return MeterDataBlock(0, [0] * TOTAL_REGISTERS, Stats())

    def test_update_internal_writes_values(self):
        block = self._make_block()
        new_values = list(range(TOTAL_REGISTERS))
        block.update_internal(0, new_values)
        assert block.getValues(0, TOTAL_REGISTERS) == new_values

    def test_getvalues_returns_correct_slice(self):
        block = MeterDataBlock(0, list(range(TOTAL_REGISTERS)), Stats())
        assert block.getValues(2, 3) == [2, 3, 4]

    def test_setvalues_persists(self):
        block = self._make_block()
        block.setValues(4, [0xABCD, 0x1234])
        result = block.getValues(4, 2)
        assert result == [0xABCD, 0x1234]

    def test_update_internal_and_getvalues_consistent(self):
        """update_internal and getValues should access the same storage."""
        block = self._make_block()
        block.update_internal(0, [42] * TOTAL_REGISTERS)
        assert all(v == 42 for v in block.getValues(0, TOTAL_REGISTERS))


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

class TestStats:
    def test_all_counters_default_to_zero(self):
        s = Stats()
        assert s.request_count    == 0
        assert s.connection_count == 0
        assert s.error_count      == 0

    def test_request_count_can_be_incremented(self):
        s = Stats()
        s.request_count += 3
        assert s.request_count == 3

    def test_counters_are_independent(self):
        s1 = Stats()
        s2 = Stats()
        s1.request_count += 99
        assert s2.request_count == 0


# ---------------------------------------------------------------------------
# MeterSimulator defaults
# ---------------------------------------------------------------------------

class TestMeterSimulatorDefaults:
    def test_default_host(self):
        assert MeterSimulator.DEFAULT_HOST == "0.0.0.0"

    def test_default_port(self):
        assert MeterSimulator.DEFAULT_PORT == 502

    def test_default_unit_id(self):
        assert MeterSimulator.DEFAULT_UNIT_ID == 1

    def test_default_seed(self):
        assert MeterSimulator.DEFAULT_SEED == 0

    def test_default_update_interval_ms(self):
        assert MeterSimulator.DEFAULT_UPDATE_INTERVAL_MS == 100

    def test_constructor_stores_params(self):
        sim = MeterSimulator(host="127.0.0.1", port=5020, unit_id=3, seed=45)
        assert sim.host    == "127.0.0.1"
        assert sim.port    == 5020
        assert sim.unit_id == 3

    def test_stats_initialised(self):
        sim = MeterSimulator()
        assert isinstance(sim.stats, Stats)
        assert sim.stats.request_count == 0


# ---------------------------------------------------------------------------
# _request_tracer
# ---------------------------------------------------------------------------

class TestTracePdu:
    """_trace_pdu is the synchronous callback registered with pymodbus."""

    def test_single_received_pdu_increments_count(self):
        from unittest.mock import MagicMock
        sim = MeterSimulator()
        pdu = MagicMock()
        pdu.function_code = 3
        returned = sim._trace_pdu(sending=False, pdu=pdu)
        assert sim.stats.request_count == 1
        assert returned is pdu  # must return the pdu unchanged

    def test_sent_pdu_does_not_increment_count(self):
        from unittest.mock import MagicMock
        sim = MeterSimulator()
        pdu = MagicMock()
        sim._trace_pdu(sending=True, pdu=pdu)
        assert sim.stats.request_count == 0

    def test_multiple_received_pdus_accumulate(self):
        from unittest.mock import MagicMock
        sim = MeterSimulator()
        pdu = MagicMock()
        pdu.function_code = 3
        for _ in range(7):
            sim._trace_pdu(sending=False, pdu=pdu)
        assert sim.stats.request_count == 7

    def test_stats_not_shared_between_instances(self):
        from unittest.mock import MagicMock
        sim1 = MeterSimulator()
        sim2 = MeterSimulator()
        pdu  = MagicMock()
        pdu.function_code = 3
        sim1._trace_pdu(sending=False, pdu=pdu)
        assert sim1.stats.request_count == 1
        assert sim2.stats.request_count == 0


# ---------------------------------------------------------------------------
# Integration test – real TCP server (marks: integration)
# ---------------------------------------------------------------------------

_INTEGRATION_PORT = 15_502  # high port; no root required


@pytest.mark.integration
async def test_server_returns_valid_register_values():
    """
    Start the simulator, connect with an async Modbus client, read all
    holding registers, verify decoded values are in range, then stop.
    """
    from pymodbus.client import AsyncModbusTcpClient

    sim         = MeterSimulator(host="127.0.0.1", port=_INTEGRATION_PORT, unit_id=1)
    server_task = asyncio.create_task(sim.start())

    # Give the server time to start accepting connections.
    await asyncio.sleep(0.5)

    try:
        async with AsyncModbusTcpClient("127.0.0.1", port=_INTEGRATION_PORT) as client:
            result = await client.read_holding_registers(0, count=10, device_id=1)

            assert not result.isError(), f"Modbus error: {result}"
            assert len(result.registers) == TOTAL_REGISTERS

            # Decode and validate active power
            hi, lo        = result.registers[0], result.registers[1]
            active_power  = ValueGenerator.registers_to_float(hi, lo)
            assert 10.0 <= active_power <= 90.0, f"active_power={active_power}"

            # Decode and validate frequency
            hi, lo        = result.registers[2], result.registers[3]
            frequency     = ValueGenerator.registers_to_float(hi, lo)
            assert 49.9 <= frequency <= 50.1, f"frequency={frequency}"

            # Decode and validate voltage
            hi, lo        = result.registers[4], result.registers[5]
            voltage       = ValueGenerator.registers_to_float(hi, lo)
            assert 225.0 <= voltage <= 235.0, f"voltage={voltage}"

            # Relay and status
            assert result.registers[8] == 1, "relay_state should be 1 (closed)"
            assert result.registers[9] == 0, "device_status should be 0 (healthy)"

    finally:
        await sim.stop()
        try:
            await asyncio.wait_for(server_task, timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            server_task.cancel()
