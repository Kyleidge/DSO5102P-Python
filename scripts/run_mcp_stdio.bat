@echo off
rem MCP server over stdio for MCP clients. stdout carries protocol messages only.
rem Use the absolute path of this file in your MCP client configuration.
"%~dp0..\.venv\Scripts\hantek-mcp.exe" serve --transport stdio %*
