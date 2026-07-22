#!/usr/bin/env python3
"""Simple GUI launcher for the Modbus simulator v3."""
from __future__ import annotations

import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

CLIENT_IPS_TOKEN = "CLIENT_IPS="


class SimulatorGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Modbus Meter Simulator V3")
        self.geometry("1020x660")
        self.minsize(900, 540)

        self._proc: subprocess.Popen[str] | None = None
        self._log_queue: queue.Queue[str] = queue.Queue()

        self._script_path = Path(__file__).with_name("modbus_meter_simulator_v3.py")
        self._python_exe = Path(sys.executable)

        self._build_ui()
        self._schedule_log_drain()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=10)
        top.pack(fill=tk.X)

        self.host_var = tk.StringVar(value="0.0.0.0")
        self.port_var = tk.StringVar(value="5021")
        self.unit_var = tk.StringVar(value="1")
        self.seed_var = tk.StringVar(value="0")
        self.interval_var = tk.StringVar(value="100")
        self.display_var = tk.StringVar(value="1")

        self.verbose_var = tk.BooleanVar(value=False)
        self.show_values_var = tk.BooleanVar(value=True)

        self._add_labeled_entry(top, "Host", self.host_var, 0)
        self._add_labeled_entry(top, "Port", self.port_var, 1)
        self._add_labeled_entry(top, "Unit ID", self.unit_var, 2)
        self._add_labeled_entry(top, "Seed", self.seed_var, 3)
        self._add_labeled_entry(top, "Update (ms)", self.interval_var, 4)
        self._add_labeled_entry(top, "Display (s)", self.display_var, 5)

        options = ttk.Frame(self, padding=(10, 0, 10, 6))
        options.pack(fill=tk.X)

        ttk.Checkbutton(options, text="Verbose", variable=self.verbose_var).pack(side=tk.LEFT, padx=(0, 20))
        ttk.Checkbutton(options, text="Show values", variable=self.show_values_var).pack(side=tk.LEFT)

        buttons = ttk.Frame(self, padding=(10, 0, 10, 8))
        buttons.pack(fill=tk.X)

        self.start_btn = ttk.Button(buttons, text="Start", command=self.start_simulator)
        self.start_btn.pack(side=tk.LEFT)

        self.stop_btn = ttk.Button(buttons, text="Stop", command=self.stop_simulator, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=8)

        ttk.Button(buttons, text="Clear Log", command=self._clear_log).pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(buttons, textvariable=self.status_var).pack(side=tk.RIGHT)

        clients = ttk.LabelFrame(self, text="Connected Client IPs", padding=(10, 6, 10, 8))
        clients.pack(fill=tk.X, padx=10, pady=(0, 8))
        self.connected_ips_var = tk.StringVar(value="none")
        ttk.Label(clients, textvariable=self.connected_ips_var).pack(anchor=tk.W)

        log_frame = ttk.Frame(self, padding=(10, 0, 10, 10))
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(log_frame, wrap=tk.NONE, height=24)
        self.log_text.grid(row=0, column=0, sticky="nsew")

        yscroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        yscroll.grid(row=0, column=1, sticky="ns")

        xscroll = ttk.Scrollbar(log_frame, orient=tk.HORIZONTAL, command=self.log_text.xview)
        xscroll.grid(row=1, column=0, sticky="ew")

        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self._bind_scroll_events()

    @staticmethod
    def _add_labeled_entry(parent: ttk.Frame, label: str, var: tk.StringVar, column: int) -> None:
        frame = ttk.Frame(parent)
        frame.grid(row=0, column=column, padx=5, sticky=tk.W)
        ttk.Label(frame, text=label).pack(anchor=tk.W)
        ttk.Entry(frame, width=12, textvariable=var).pack(anchor=tk.W)

    def _build_command(self) -> list[str]:
        if not self._script_path.exists():
            raise FileNotFoundError(f"Simulator script not found: {self._script_path}")

        cmd = [
            str(self._python_exe),
            str(self._script_path),
            "--host",
            self.host_var.get().strip(),
            "--port",
            self.port_var.get().strip(),
            "--unit-id",
            self.unit_var.get().strip(),
            "--seed",
            self.seed_var.get().strip(),
            "--interval",
            self.interval_var.get().strip(),
            "--display-interval",
            self.display_var.get().strip(),
        ]

        if self.verbose_var.get():
            cmd.append("--verbose")
        if not self.show_values_var.get():
            cmd.append("--hide-values")

        return cmd

    def start_simulator(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return

        try:
            cmd = self._build_command()
        except Exception as exc:
            messagebox.showerror("Start failed", str(exc))
            return

        self.connected_ips_var.set("none")
        self._append_log("\n--- Starting simulator V3 ---\n")
        self._append_log("Command: " + " ".join(cmd) + "\n")

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            messagebox.showerror("Start failed", str(exc))
            self._append_log(f"Failed to start: {exc}\n")
            self._proc = None
            return

        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self.status_var.set("Running")

        reader = threading.Thread(target=self._read_process_output, daemon=True)
        reader.start()

    def stop_simulator(self) -> None:
        if self._proc is None:
            return

        if self._proc.poll() is None:
            self._append_log("\n--- Stopping simulator ---\n")
            self._proc.terminate()

        self._set_stopped_state()

    def _read_process_output(self) -> None:
        assert self._proc is not None
        stream = self._proc.stdout
        if stream is not None:
            for line in stream:
                self._log_queue.put(line)

        rc = self._proc.wait()
        self._log_queue.put(f"\n--- Simulator exited (code {rc}) ---\n")
        self._log_queue.put("__STATE_STOPPED__\n")

    def _schedule_log_drain(self) -> None:
        self._drain_log_queue()
        self.after(100, self._schedule_log_drain)

    def _drain_log_queue(self) -> None:
        while True:
            try:
                line = self._log_queue.get_nowait()
            except queue.Empty:
                break

            if line == "__STATE_STOPPED__\n":
                self._set_stopped_state()
                continue

            self._process_status_line(line)
            self._append_log(line)

    def _process_status_line(self, line: str) -> None:
        if CLIENT_IPS_TOKEN not in line:
            return

        payload = line.split(CLIENT_IPS_TOKEN, 1)[1].strip()
        if not payload:
            payload = "none"
        self.connected_ips_var.set(payload)

    def _append_log(self, text: str) -> None:
        at_bottom = self.log_text.yview()[1] >= 0.999
        self.log_text.insert(tk.END, text)
        if at_bottom:
            self.log_text.see(tk.END)

    def _bind_scroll_events(self) -> None:
        self.log_text.bind("<MouseWheel>", self._on_mousewheel_vertical)
        self.log_text.bind("<Shift-MouseWheel>", self._on_mousewheel_horizontal)

    def _on_mousewheel_vertical(self, event: tk.Event) -> str:
        self.log_text.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def _on_mousewheel_horizontal(self, event: tk.Event) -> str:
        self.log_text.xview_scroll(int(-event.delta / 120), "units")
        return "break"

    def _clear_log(self) -> None:
        self.log_text.delete("1.0", tk.END)

    def _set_stopped_state(self) -> None:
        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)
        self.status_var.set("Stopped")

    def _on_close(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
        self.destroy()


if __name__ == "__main__":
    app = SimulatorGUI()
    app.mainloop()
