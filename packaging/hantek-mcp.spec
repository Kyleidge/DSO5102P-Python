# PyInstaller spec (optional deliverable). Build on Windows with:
#   uv sync --extra gui --extra exe
#   uv run pyinstaller packaging/hantek-mcp.spec --noconfirm
# Produces dist/hantek-mcp/hantek-mcp.exe (CLI + MCP server; `hantek-mcp.exe gui` opens the GUI).
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 - SPECPATH is injected by PyInstaller

hidden = collect_submodules("hantek_mcp") + collect_submodules("mcp") + collect_submodules("uvicorn")
datas = collect_data_files("hantek_mcp") + collect_data_files("libusb_package")

a = Analysis(
    [os.path.join(ROOT, "src", "hantek_mcp", "__main__.py")],
    pathex=[os.path.join(ROOT, "src")],
    hiddenimports=hidden,
    datas=datas,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="hantek-mcp", console=True)
coll = COLLECT(exe, a.binaries, a.datas, name="hantek-mcp")
