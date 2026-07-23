# Modbus TCP Meter Simulator

A single-device Modbus TCP electrical-meter simulator for automated SiteSee2 testing.

## Features

- Deterministic, repeatable sinusoidal waveforms – the same `--seed` always produces
  the same value sequence across restarts
- Holding registers updated every 100 ms by default (configurable)
- Input registers (FC 04) mirror holding registers at the same addresses
- Configurable IP address, TCP port, and Modbus unit ID
- Logs every client connection, disconnection, and request (DEBUG level)
- Tracks `request_count`, `connection_count`, and `error_count`
- Graceful SIGINT / SIGTERM shutdown
- Full pytest coverage for value generation and register mapping

---

## Register Map

| Address | Words | Type    | Unit | Description                         |
|---------|-------|---------|------|-------------------------------------|
| 0       | 2     | float32 | kW   | Active power – 3-phase sum (10..90) |
| 2       | 2     | float32 | Hz   | Mains frequency (49.9..50.1)        |
| 4       | 2     | float32 | V    | Phase-A voltage, L-N RMS (225..235) |
| 6       | 2     | float32 | A    | Phase-A RMS current (derived)       |
| 8       | 1     | uint16  | –    | Relay state: 1 = closed, 0 = open   |
| 9       | 1     | uint16  | –    | Device status: 0 = healthy          |

**Word order:** big-endian (high word at the lower address).  
Read holding registers with **FC 03** or input registers with **FC 04**.

### External Device Tag List

Use this table when configuring another device, SCADA client, HMI, or PLC to read from the simulator.

| Parameter      | Tag type      | Unit | Data type | Read function code | Multiplier | Address | Words |
|----------------|---------------|------|-----------|--------------------|------------|---------|-------|
| active_power   | Analog input  | kW   | float32   | FC03 or FC04       | 1          | 0       | 2     |
| frequency      | Analog input  | Hz   | float32   | FC03 or FC04       | 1          | 2       | 2     |
| voltage        | Analog input  | V    | float32   | FC03 or FC04       | 1          | 4       | 2     |
| current        | Analog input  | A    | float32   | FC03 or FC04       | 1          | 6       | 2     |
| relay_state    | Digital status| -    | uint16    | FC03 or FC04       | 1          | 8       | 1     |
| device_status  | Digital status| -    | uint16    | FC03 or FC04       | 1          | 9       | 1     |

Notes:

- `FC03` reads holding registers.
- `FC04` reads input registers mirrored from the same values.
- `float32` values use big-endian word order.
- `relay_state` returns `1` for closed and `0` for open.
- `device_status` returns `0` for healthy.

---

## Installation

```powershell
# Create and activate a virtual environment (recommended)
python -m venv .venv
.venv\Scripts\Activate.ps1          # Windows PowerShell
# source .venv/bin/activate          # Linux / macOS

pip install -r requirements.txt
```

---

## Running the Simulator

```powershell
# Default: 0.0.0.0:502, unit ID 1, seed 0, 100 ms update interval
python modbus_meter_simulator.py

# V2 (localhost default) using venv python directly
& ".\.venv\Scripts\python.exe" ".\modbus_meter_simulator_v2.py" --port 5021 --display-interval 1

# Custom settings
python modbus_meter_simulator.py `
    --host 127.0.0.1 `
    --port 5020 `
    --unit-id 2 `
    --seed 42 `
    --interval 200 `
    --verbose

# Full help
python modbus_meter_simulator.py --help

# V2 full help
& ".\.venv\Scripts\python.exe" ".\modbus_meter_simulator_v2.py" --help

# V3 full help
& ".\.venv\Scripts\python.exe" ".\modbus_meter_simulator_v3.py" --help
```

> **Note:** TCP port 502 requires root / Administrator privileges.  
> Use `--port 5020` (or any port > 1023) for development without elevation.

### Command-line options

| Flag            | Default   | Description                                    |
|-----------------|-----------|------------------------------------------------|
| `--host ADDR`   | `0.0.0.0` | Bind address (`0.0.0.0` = all interfaces)      |
| `--port N`      | `502`     | TCP port                                       |
| `--unit-id N`   | `1`       | Modbus unit / slave ID (1–247)                 |
| `--seed N`      | `0`       | Waveform phase seed (0–359)                    |
| `--interval MS` | `100`     | Register update interval in milliseconds       |
| `-v/--verbose`  | off       | Enable DEBUG-level logging                     |

### V2 additional options

| Flag                  | Default | Description                                              |
|-----------------------|---------|----------------------------------------------------------|
| `--display-interval`  | `1.0`   | Seconds between printed simulated-value lines            |
| `--hide-values`       | off     | Disable periodic simulated-value logging                 |

---

## V2 Execution (Recommended)

`modbus_meter_simulator_v2.py` is a copy of the simulator configured for localhost-first workflows.

### Run directly from venv

```powershell
cd "C:\Users\ayomide.adesiyan\OneDrive - Endeco-Technologies\Documents\PYTHON_WORK\Python"
& ".\.venv\Scripts\python.exe" ".\modbus_meter_simulator_v2.py" --port 5021 --display-interval 1
```

Expected startup lines:

- `Meter simulator listening on 127.0.0.1:5021`
- `Simulated values: active_power=... | frequency=... | voltage=... | current=... | relay_state=1 | device_status=0`

### Run via batch launcher

```powershell
.\run_modbus_simulator_v2.bat
```

The batch file already points to `.venv\Scripts\python.exe` and runs V2 with sensible defaults.

### Common PowerShell path fix

Use `.\.venv` (current folder), not `..venv` (parent folder).

Correct:

```powershell
& ".\.venv\Scripts\python.exe" ".\modbus_meter_simulator_v2.py" --port 5021 --display-interval 1
```

Incorrect:

```powershell
& "..venv\Scripts\python.exe" ...
```

---

## GUI Execution (V2)

A desktop GUI launcher is provided for start/stop and live log viewing.

### Files

- `modbus_simulator_v2_gui.py`
- `run_modbus_simulator_v2_gui.bat`

### Start GUI

Option 1 (double-click):

- `run_modbus_simulator_v2_gui.bat`

Option 2 (PowerShell):

```powershell
.\run_modbus_simulator_v2_gui.bat
```

Option 3 (run GUI script directly from venv):

```powershell
& ".\.venv\Scripts\python.exe" ".\modbus_simulator_v2_gui.py"
```

GUI features:

- Host/Port/Unit ID/Seed/interval input fields
- Start and Stop buttons
- Verbose and show-values toggles
- Live log panel with simulated parameter readings

---

## V3 Execution (External Client Ready)

`modbus_meter_simulator_v3.py` extends V2 with external-client connectivity and
connected-IP tracking.

### Files

- `modbus_meter_simulator_v3.py`
- `modbus_simulator_v3_gui.py`
- `run_modbus_simulator_v3.bat`
- `run_modbus_simulator_v3_gui.bat`

### Key behavior changes in V3

- Default bind host is `0.0.0.0` so external Modbus TCP clients can connect
- Active client IP addresses are tracked in real time
- Simulator emits structured connection-state lines consumed by the GUI

### Run V3 simulator directly

```powershell
& ".\.venv\Scripts\python.exe" ".\modbus_meter_simulator_v3.py" --port 5021 --display-interval 1
```

### Run V3 simulator via batch launcher

```powershell
.\run_modbus_simulator_v3.bat
```

### Run V3 GUI

Option 1 (double-click):

- `run_modbus_simulator_v3_gui.bat`

Option 2 (PowerShell):

```powershell
.\run_modbus_simulator_v3_gui.bat
```

Option 3 (direct script):

```powershell
& ".\.venv\Scripts\python.exe" ".\modbus_simulator_v3_gui.py"
```

### V3 external connection process

1. Start `modbus_simulator_v3_gui.py`.
2. Keep `Host` as `0.0.0.0` (or set your LAN adapter IP).
3. Set `Port` to a non-privileged development port (for example `5021`).
4. Click **Start**.
5. From another machine or app, connect using this PC's IP and the configured port.
6. Watch **Connected Client IPs** in the GUI update live.
7. Confirm traffic in the log panel (read requests and simulated values).

If no external client can connect:

- Check Windows Firewall inbound rule for your selected TCP port
- Verify the client targets this machine's reachable LAN IP (not `127.0.0.1`)
- Ensure the selected host/port are not already in use

### V3 Windows connectivity verification runbook

Use this process when V3 appears to run but an external Modbus TCP client cannot connect.

1. Confirm this PC's active IPv4 addresses.

```powershell
ipconfig
```

Record all active adapter IPv4 addresses (for example `192.168.0.104`, `192.168.1.250`).

2. Start the simulator on all interfaces.

```powershell
& ".\.venv\Scripts\python.exe" ".\modbus_meter_simulator_v3.py" --host 0.0.0.0 --port 5021 --display-interval 1
```

Expected log lines include:

- `Meter simulator V3 listening on 0.0.0.0:5021 ...`
- `Server listening.`

3. Verify that the port is actually listening.

```powershell
Get-NetTCPConnection -LocalPort 5021 -State Listen | Select-Object LocalAddress,LocalPort,OwningProcess
```

Expected: `LocalAddress` includes `0.0.0.0` and `LocalPort` is `5021`.

4. Validate local TCP reachability on the chosen LAN IP.

```powershell
Test-NetConnection -ComputerName 192.168.0.104 -Port 5021 | Select-Object ComputerName,RemotePort,TcpTestSucceeded
```

Expected: `TcpTestSucceeded` is `True`.

5. If external clients still fail, inspect inbound firewall allowance.

```powershell
Get-NetFirewallRule -PolicyStore ActiveStore |
   Where-Object { $_.Direction -eq 'Inbound' -and $_.Enabled -eq 'True' -and $_.Action -eq 'Allow' } |
   Get-NetFirewallPortFilter |
   Where-Object { $_.Protocol -eq 'TCP' -and ($_.LocalPort -eq '5021' -or $_.LocalPort -eq 'Any') } |
   Select-Object InstanceID,Protocol,LocalPort
```

If no suitable rule is present for your network profile, add an inbound allow rule for TCP port `5021`.

6. Configure the external client with the correct target.

- Protocol: `Modbus TCP`
- Port: `5021`
- Unit ID: `1`
- Host IP: use the IP on the same subnet as the client device

Examples:

- Client on `192.168.0.x` network -> use simulator IP `192.168.0.104`
- Client on `192.168.1.x` network -> use simulator IP `192.168.1.250`

7. Confirm live connection state in the V3 GUI.

- The **Local IP(s) for external clients** line lists candidate addresses.
- The **Currently connected** line updates when a client connects.

---

## Connecting SiteSee2

1. Open the SiteSee2 **Device Configuration** screen.
2. Select **Modbus TCP** as the communication protocol.
3. Fill in the connection fields:

   | SiteSee2 field | Value                                         |
   |----------------|-----------------------------------------------|
   | IP address     | Host running the simulator (e.g. `127.0.0.1`) |
   | TCP port       | Same as `--port` (default `502`)              |
   | Unit ID        | Same as `--unit-id` (default `1`)             |
   | Word order     | Big-endian (high word first)                  |

4. Map the registers using the table in the **Register Map** section above.
5. Start the SiteSee2 poll. The simulator console will log the connection and
   each request at DEBUG level (pass `--verbose` to see them).

---

## Running Tests

```powershell
# Unit tests only (no server started, fast)
pytest

# Verbose output
pytest -v

# Include the integration test (starts a real server on localhost:15502)
pytest -m integration -v

# All tests
pytest -m "integration or not integration" -v
```

The test suite covers:

| Area                    | What is tested                                          |
|-------------------------|---------------------------------------------------------|
| `ValueGenerator` ranges | All values stay within documented healthy-device bounds |
| Determinism             | Same seed + same time → identical values                |
| Periodicity             | Values repeat exactly every `CYCLE_PERIOD_S` seconds    |
| Float encoding          | `float_to_registers` / `registers_to_float` round-trip  |
| Register map            | No address gaps, no overlaps, correct total word count  |
| `_pack_registers`       | Each field decoded from raw words matches source value  |
| `MeterDataBlock`        | `update_internal` writes, `getValues` slicing           |
| `Stats`                 | Default zeros, independent per instance                 |
| `_request_tracer`       | Increments `request_count` on every call                |
| Integration (optional)  | Live FC 03 read returns in-range values                 |

---

## Determinism

Values are computed from wall-clock time:

```
phase        = (t / 60.0) x 2pi  +  (seed % 360) x (pi / 180)
active_power = 50 + 40 x sin(phase)       kW   (period 60 s)
frequency    = 50 + 0.1 x sin(phase x 3)  Hz   (period 20 s)
voltage      = 230 + 5  x sin(phase x 2)  V    (period 30 s)
current      = active_power x 1000 / (3 x voltage x 0.95)  A
```

The same `--seed` always produces the same waveform shape, making automated
assertions reliable across simulator restarts.

---

## Out of Scope (v1)

- Fault / exception injection
- Multiple simultaneous simulated devices
- High-load / stress testing

---

## Update Log

- Added `modbus_meter_simulator_v2.py` (localhost-first simulator copy)
- Added periodic simulated-parameter display output in V2
- Added `run_modbus_simulator_v2.bat` for direct execution
- Added `modbus_simulator_v2_gui.py` desktop launcher
- Added `run_modbus_simulator_v2_gui.bat` for one-click GUI start
- Added `modbus_meter_simulator_v3.py` with external-client support (`0.0.0.0` default bind)
- Added real-time connected-client IP tracking in V3
- Added `modbus_simulator_v3_gui.py` with connected-IP display panel
- Added `run_modbus_simulator_v3.bat` and `run_modbus_simulator_v3_gui.bat`

## Keeping This README Current

When behavior, flags, defaults, launcher scripts, or file names change:

1. Update the matching command examples in this README.
2. Update the V2/GUI sections in the same change set.
3. Add one line in **Update Log** summarizing what changed.
