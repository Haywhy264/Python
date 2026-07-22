@echo off
setlocal
set SCRIPT_DIR=%~dp0
"C:\Python313\python.exe" "%SCRIPT_DIR%opc_tag_client.py" %*
