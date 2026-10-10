@echo off
rem Launch the Hantek DSO5102P GUI from the virtual environment created by install_windows.ps1.
rem Pass --simulate to use the built-in simulator, --mcp-http to host the MCP endpoint.
setlocal
set "VENV=%~dp0..\.venv"
if not exist "%VENV%\Scripts\hantek-mcp.exe" (
  echo hantek-mcp is not installed. Run scripts\install_windows.ps1 first.
  exit /b 1
)
"%VENV%\Scripts\hantek-mcp.exe" gui %*
