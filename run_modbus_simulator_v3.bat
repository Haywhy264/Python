@echo off
setlocal
set SCRIPT_DIR=%~dp0
"%SCRIPT_DIR%.venv\Scripts\python.exe" "%SCRIPT_DIR%modbus_meter_simulator_v3.py" --port 5021 --display-interval 1 %*
