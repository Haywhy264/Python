#!/usr/bin/env python3
"""
Modbus TCP Load Tester — GUI
============================
A tkinter front-end for stress-testing Modbus TCP servers.

Features
--------
* Configurable target IP, port, Unit ID, concurrent clients, polls per client,
  register address, and register count.
* TCP Keep-Alive — keeps persistent connections alive across idle periods
  (survives NAT / firewall timeouts).  Idle probe delay and probe interval are
  configurable from the GUI.
* Auto-reconnect — pymodbus re-establishes dropped connections automatically,
  with configurable initial delay and exponential back-off cap.
* Live progress bar and per-request latency metrics
  (min / avg / median / p95 / p99 / max).
* Stop button for clean mid-run cancellation.

Changelog
---------
v1.1 — Added TCP keep-alive support (SO_KEEPALIVE / SIO_KEEPALIVE_VALS),
       configurable reconnect delay, and Connection Settings panel in the GUI.
v1.0 — Initial release.
"""
from __future__ import annotations

import asyncio
import queue
import socket
import statistics
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from pymodbus.client import AsyncModbusTcpClient

def _apply_keepalive(
    sock: socket.socket,
    idle_s: int = 10,
    intvl_s: int = 5,
    cnt: int = 3,
) -> None:
    """Enable TCP keepalive on *sock*.

    Parameters
    ----------
    idle_s:
        Seconds of inactivity before the first keepalive probe is sent.
    intvl_s:
        Seconds between subsequent probes (Linux / macOS).
    cnt:
        Number of unanswered probes before the connection is dropped (Linux).
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

        if hasattr(socket, "TCP_KEEPIDLE"):       # Linux / macOS
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE,  idle_s)
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, intvl_s)
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT,   cnt)

        # Windows — one ioctl call: (enable, idle_ms, interval_ms)
        if hasattr(socket, "SIO_KEEPALIVE_VALS"):
            sock.ioctl(
                socket.SIO_KEEPALIVE_VALS,
                (1, idle_s * 1000, intvl_s * 1000),
            )
    except OSError:
        pass  # keepalive is best-effort; never block the test

# ---------------------------------------------------------------------------
# Sentinel tokens sent through the message queue
# ---------------------------------------------------------------------------
_MSG_DONE    = "__DONE__"
_MSG_ABORTED = "__ABORTED__"


class LoadTestGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Modbus TCP Load Tester")
        self.geometry("860x660")
        self.minsize(700, 500)

        self._msg_queue: queue.Queue[str] = queue.Queue()
        self._stop_event = threading.Event()
        self._test_thread: threading.Thread | None = None

        self._build_ui()
        self._schedule_drain()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # ── Config panel ─────────────────────────────────────────────
        config = ttk.LabelFrame(self, text="Target & Test Parameters", padding=(10, 6))
        config.pack(fill=tk.X, padx=10, pady=(10, 4))

        row0 = ttk.Frame(config)
        row0.pack(fill=tk.X)
        row1 = ttk.Frame(config)
        row1.pack(fill=tk.X, pady=(6, 0))

        self.ip_var       = tk.StringVar(value="127.0.0.1")
        self.port_var     = tk.StringVar(value="5021")
        self.unit_var     = tk.StringVar(value="1")
        self.clients_var  = tk.StringVar(value="10")
        self.polls_var    = tk.StringVar(value="20")
        self.register_var = tk.StringVar(value="0")
        self.count_var    = tk.StringVar(value="2")

        self._entry(row0, "Target IP",       self.ip_var,       0)
        self._entry(row0, "Port",            self.port_var,     1)
        self._entry(row0, "Unit ID",         self.unit_var,     2)
        self._entry(row1, "Clients",         self.clients_var,  0)
        self._entry(row1, "Polls / client",  self.polls_var,    1)
        self._entry(row1, "Register addr",   self.register_var, 2)
        self._entry(row1, "Reg count",       self.count_var,    3)

        # ── Connection Settings ───────────────────────────────────────
        conn = ttk.LabelFrame(
            self, text="Connection Settings", padding=(10, 6)
        )
        conn.pack(fill=tk.X, padx=10, pady=(0, 4))

        conn_row = ttk.Frame(conn)
        conn_row.pack(fill=tk.X)

        self.ka_enabled_var   = tk.BooleanVar(value=True)
        self.ka_idle_var      = tk.StringVar(value="10")
        self.ka_intvl_var     = tk.StringVar(value="5")
        self.reconnect_var    = tk.StringVar(value="0.5")
        self.reconnect_max_var = tk.StringVar(value="5.0")

        ttk.Checkbutton(
            conn_row, text="Enable TCP Keep-Alive",
            variable=self.ka_enabled_var,
            command=self._toggle_keepalive_fields,
        ).grid(row=0, column=0, padx=(0, 20), sticky=tk.W)

        self._ka_entries: list[ttk.Entry] = []
        for col, (lbl, var) in enumerate(
            [
                ("Idle probe (s)",   self.ka_idle_var),
                ("Probe interval (s)", self.ka_intvl_var),
                ("Reconnect delay (s)", self.reconnect_var),
                ("Reconnect max (s)",  self.reconnect_max_var),
            ],
            start=1,
        ):
            cell = ttk.Frame(conn_row)
            cell.grid(row=0, column=col, padx=(0, 16), sticky=tk.W)
            ttk.Label(cell, text=lbl).pack(anchor=tk.W)
            e = ttk.Entry(cell, width=10, textvariable=var)
            e.pack(anchor=tk.W)
            self._ka_entries.append(e)

        ka_note = (
            "Keep-Alive prevents idle connections from being dropped by "
            "firewalls or NAT devices.  Reconnect delay controls how quickly "
            "pymodbus re-establishes a dropped connection."
        )
        ttk.Label(conn, text=ka_note, foreground="#555555",
                  wraplength=780).pack(anchor=tk.W, pady=(4, 0))

        # ── Buttons & status ─────────────────────────────────────────
        btn_bar = ttk.Frame(self, padding=(10, 4))
        btn_bar.pack(fill=tk.X)

        self.start_btn = ttk.Button(btn_bar, text="Start Test", command=self._start)
        self.start_btn.pack(side=tk.LEFT)

        self.stop_btn = ttk.Button(
            btn_bar, text="Stop", command=self._stop, state=tk.DISABLED
        )
        self.stop_btn.pack(side=tk.LEFT, padx=8)

        ttk.Button(btn_bar, text="Clear Log", command=self._clear_log).pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(btn_bar, textvariable=self.status_var).pack(side=tk.RIGHT)

        # ── Progress bar ─────────────────────────────────────────────
        prog_frame = ttk.Frame(self, padding=(10, 0))
        prog_frame.pack(fill=tk.X)

        self.progress_var = tk.IntVar(value=0)
        self.progress_max = tk.IntVar(value=100)
        self.progress_bar = ttk.Progressbar(
            prog_frame,
            variable=self.progress_var,
            maximum=100,
            mode="determinate",
        )
        self.progress_bar.pack(fill=tk.X)

        # ── Results panel ─────────────────────────────────────────────
        results = ttk.LabelFrame(self, text="Last Run Results", padding=(10, 6))
        results.pack(fill=tk.X, padx=10, pady=(6, 4))

        self._result_vars: dict[str, tk.StringVar] = {}
        result_fields = [
            ("Total requests",  "total"),
            ("Successful",      "success"),
            ("Errors",          "errors"),
            ("Duration (s)",    "duration"),
            ("Throughput",      "throughput"),
            ("Latency min",     "lat_min"),
            ("Latency avg",     "lat_avg"),
            ("Latency median",  "lat_med"),
            ("Latency p95",     "lat_p95"),
            ("Latency p99",     "lat_p99"),
            ("Latency max",     "lat_max"),
        ]
        cols = 4
        for idx, (label, key) in enumerate(result_fields):
            var = tk.StringVar(value="—")
            self._result_vars[key] = var
            col = idx % cols
            row = idx // cols
            cell = ttk.Frame(results)
            cell.grid(row=row, column=col, padx=(0, 20), pady=2, sticky=tk.W)
            ttk.Label(cell, text=label + ":").pack(anchor=tk.W)
            ttk.Label(cell, textvariable=var, foreground="#0066cc").pack(anchor=tk.W)

        # ── Log area ──────────────────────────────────────────────────
        log_frame = ttk.Frame(self, padding=(10, 0, 10, 10))
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(log_frame, wrap=tk.NONE, height=14)
        self.log_text.grid(row=0, column=0, sticky="nsew")

        ys = ttk.Scrollbar(log_frame, orient=tk.VERTICAL,   command=self.log_text.yview)
        xs = ttk.Scrollbar(log_frame, orient=tk.HORIZONTAL, command=self.log_text.xview)
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)

        self.log_text.bind("<MouseWheel>",        self._scroll_y)
        self.log_text.bind("<Shift-MouseWheel>",  self._scroll_x)

    def _toggle_keepalive_fields(self) -> None:
        """Enable or disable keepalive entry fields based on the checkbox."""
        state = tk.NORMAL if self.ka_enabled_var.get() else tk.DISABLED
        for e in self._ka_entries:
            e.configure(state=state)

    @staticmethod
    def _entry(parent: ttk.Frame, label: str, var: tk.StringVar, col: int) -> None:
        frame = ttk.Frame(parent)
        frame.grid(row=0, column=col, padx=(0, 16), sticky=tk.W)
        ttk.Label(frame, text=label).pack(anchor=tk.W)
        ttk.Entry(frame, width=14, textvariable=var).pack(anchor=tk.W)

    # ------------------------------------------------------------------
    # Load-test runner (background thread)
    # ------------------------------------------------------------------

    def _start(self) -> None:
        try:
            cfg = self._read_config()
        except ValueError as exc:
            messagebox.showerror("Invalid input", str(exc))
            return

        self._stop_event.clear()
        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self.status_var.set("Running…")
        self.progress_var.set(0)
        self.progress_bar.configure(maximum=cfg["total"])
        for var in self._result_vars.values():
            var.set("—")

        ka_line = (
            f"  Keep-Alive : enabled  "
            f"idle={cfg['ka_idle']}s  interval={cfg['ka_intvl']}s"
            if cfg["ka_enabled"]
            else "  Keep-Alive : disabled"
        )
        self._log(
            f"\n{'─'*60}\n"
            f"  Load test started\n"
            f"  Target     : {cfg['ip']}:{cfg['port']}  unit={cfg['unit']}\n"
            f"  Clients    : {cfg['clients']}  ×  {cfg['polls']} polls  "
            f"=  {cfg['total']} requests\n"
            f"  Register   : {cfg['register']}  count={cfg['reg_count']}\n"
            f"{ka_line}\n"
            f"  Reconnect  : delay={cfg['reconnect_delay']}s  "
            f"max={cfg['reconnect_max']}s\n"
            f"{'─'*60}\n"
        )

        self._test_thread = threading.Thread(
            target=self._run_async_test, args=(cfg,), daemon=True
        )
        self._test_thread.start()

    def _stop(self) -> None:
        self._stop_event.set()
        self.stop_btn.configure(state=tk.DISABLED)
        self.status_var.set("Stopping…")

    def _read_config(self) -> dict:
        def _int(name: str, var: tk.StringVar) -> int:
            val = var.get().strip()
            if not val.isdigit() or int(val) <= 0:
                raise ValueError(f"'{name}' must be a positive integer.")
            return int(val)

        ip   = self.ip_var.get().strip()
        port = _int("Port", self.port_var)
        unit = _int("Unit ID", self.unit_var)
        clients  = _int("Clients", self.clients_var)
        polls    = _int("Polls / client", self.polls_var)
        register = int(self.register_var.get().strip() or "0")
        reg_count = _int("Reg count", self.count_var)
        if not ip:
            raise ValueError("Target IP cannot be empty.")

        def _pos_float(name: str, var: tk.StringVar) -> float:
            try:
                v = float(var.get().strip())
                if v <= 0:
                    raise ValueError()
                return v
            except ValueError:
                raise ValueError(f"'{name}' must be a positive number.")

        ka_enabled      = self.ka_enabled_var.get()
        ka_idle         = _int("Idle probe (s)",       self.ka_idle_var)
        ka_intvl        = _int("Probe interval (s)",   self.ka_intvl_var)
        reconnect_delay = _pos_float("Reconnect delay", self.reconnect_var)
        reconnect_max   = _pos_float("Reconnect max",   self.reconnect_max_var)

        return {
            "ip": ip, "port": port, "unit": unit,
            "clients": clients, "polls": polls,
            "register": register, "reg_count": reg_count,
            "total": clients * polls,
            "ka_enabled":      ka_enabled,
            "ka_idle":         ka_idle,
            "ka_intvl":        ka_intvl,
            "reconnect_delay": reconnect_delay,
            "reconnect_max":   reconnect_max,
        }

    def _run_async_test(self, cfg: dict) -> None:
        """Entry point for the background thread; drives the asyncio event loop."""
        asyncio.run(self._async_test(cfg))

    async def _async_test(self, cfg: dict) -> None:
        latencies: list[float] = []
        errors = 0
        lock = asyncio.Lock()
        completed = 0

        async def client_task(cid: int) -> None:
            nonlocal errors, completed

            client = AsyncModbusTcpClient(
                cfg["ip"],
                port=cfg["port"],
                reconnect_delay=cfg["reconnect_delay"],
                reconnect_delay_max=cfg["reconnect_max"],
                timeout=10,
            )
            try:
                await client.connect()

                # Apply TCP keepalive so idle connections survive NAT/firewall timeouts
                sock = (
                    client.transport.get_extra_info("socket")
                    if client.transport
                    else None
                )
                if sock is not None and cfg["ka_enabled"]:
                    _apply_keepalive(
                        sock,
                        idle_s=cfg["ka_idle"],
                        intvl_s=cfg["ka_intvl"],
                    )
                    self._msg_queue.put(
                        f"  [client {cid:>3}] connected "
                        f"— keepalive enabled "
                        f"(idle={cfg['ka_idle']}s, interval={cfg['ka_intvl']}s)\n"
                    )
                elif sock is not None:
                    self._msg_queue.put(
                        f"  [client {cid:>3}] connected — keepalive disabled\n"
                    )

                for _ in range(cfg["polls"]):
                    if self._stop_event.is_set():
                        return
                    t0 = time.perf_counter()
                    result = await client.read_holding_registers(
                        address=cfg["register"],
                        count=cfg["reg_count"],
                        device_id=cfg["unit"],
                    )
                    ms = (time.perf_counter() - t0) * 1000.0
                    async with lock:
                        if result.isError():
                            errors += 1
                            self._msg_queue.put(
                                f"  [client {cid:>3}] ERROR: {result}\n"
                            )
                        else:
                            latencies.append(ms)
                        completed += 1
                        self._msg_queue.put(f"__PROGRESS__{completed}")
            except Exception as exc:
                async with lock:
                    errors += 1
                    completed += cfg["polls"]
                    self._msg_queue.put(f"  [client {cid:>3}] EXCEPTION: {exc}\n")
                    self._msg_queue.put(f"__PROGRESS__{completed}")
            finally:
                client.close()

        t_start = time.perf_counter()
        await asyncio.gather(*[client_task(i) for i in range(cfg["clients"])])
        total_time = time.perf_counter() - t_start

        # Build result dict and push to queue
        total_req   = cfg["total"]
        success_req = len(latencies)
        throughput  = total_req / total_time if total_time > 0 else 0

        result_data: dict[str, str] = {
            "total":      str(total_req),
            "success":    str(success_req),
            "errors":     str(errors),
            "duration":   f"{total_time:.2f} s",
            "throughput": f"{throughput:.1f} req/s",
        }
        if latencies:
            sl = sorted(latencies)
            p95 = sl[max(0, int(len(sl) * 0.95) - 1)]
            p99 = sl[max(0, int(len(sl) * 0.99) - 1)]
            result_data.update({
                "lat_min": f"{min(latencies):.1f} ms",
                "lat_avg": f"{statistics.mean(latencies):.1f} ms",
                "lat_med": f"{statistics.median(latencies):.1f} ms",
                "lat_p95": f"{p95:.1f} ms",
                "lat_p99": f"{p99:.1f} ms",
                "lat_max": f"{max(latencies):.1f} ms",
            })
        else:
            for k in ("lat_min", "lat_avg", "lat_med", "lat_p95", "lat_p99", "lat_max"):
                result_data[k] = "N/A"

        # Summary log lines
        summary = (
            f"\n{'─'*60}\n"
            f"  RESULTS\n"
            f"  Total requests : {total_req}  |  "
            f"Successful: {success_req}  |  Errors: {errors}\n"
            f"  Duration       : {total_time:.2f} s  |  "
            f"Throughput: {throughput:.1f} req/s\n"
        )
        if latencies:
            summary += (
                f"  Latency        : min={result_data['lat_min']}  "
                f"avg={result_data['lat_avg']}  "
                f"p95={result_data['lat_p95']}  "
                f"max={result_data['lat_max']}\n"
            )
        summary += f"{'─'*60}\n"
        self._msg_queue.put(summary)

        # Encode results as a special token
        import json
        self._msg_queue.put(f"__RESULTS__{json.dumps(result_data)}")

        token = _MSG_ABORTED if self._stop_event.is_set() else _MSG_DONE
        self._msg_queue.put(token)

    # ------------------------------------------------------------------
    # Queue drain (runs on the Tk main thread)
    # ------------------------------------------------------------------

    def _schedule_drain(self) -> None:
        self._drain()
        self.after(80, self._schedule_drain)

    def _drain(self) -> None:
        import json
        while True:
            try:
                msg = self._msg_queue.get_nowait()
            except queue.Empty:
                break

            if msg.startswith("__PROGRESS__"):
                n = int(msg.replace("__PROGRESS__", ""))
                self.progress_var.set(n)

            elif msg.startswith("__RESULTS__"):
                data = json.loads(msg.replace("__RESULTS__", ""))
                for key, value in data.items():
                    if key in self._result_vars:
                        self._result_vars[key].set(value)

            elif msg == _MSG_DONE:
                self._set_idle("Done")

            elif msg == _MSG_ABORTED:
                self._set_idle("Stopped")

            else:
                self._log(msg)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_idle(self, status: str) -> None:
        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)
        self.status_var.set(status)

    def _log(self, text: str) -> None:
        at_bottom = self.log_text.yview()[1] >= 0.999
        self.log_text.insert(tk.END, text)
        if at_bottom:
            self.log_text.see(tk.END)

    def _clear_log(self) -> None:
        self.log_text.delete("1.0", tk.END)

    def _scroll_y(self, event: tk.Event) -> str:
        self.log_text.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def _scroll_x(self, event: tk.Event) -> str:
        self.log_text.xview_scroll(int(-event.delta / 120), "units")
        return "break"

    def _on_close(self) -> None:
        self._stop_event.set()
        self.destroy()


if __name__ == "__main__":
    app = LoadTestGUI()
    app.mainloop()
