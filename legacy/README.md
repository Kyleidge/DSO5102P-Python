# Legacy scripts (original rcr.dso5102p API)

These are the original example scripts by Roberto Carrasco, kept for backwards
compatibility. `rcr/dso5102p/DSO5102P.py` is now a thin facade over the
`hantek_mcp` library (same class and method names). Run from this directory
after installing the package (`pip install -e ..`), plus `opencv-python` for the
scripts that display images:

    python Test1.py        # echo, time, key presses, panel lock, run/stop
    python Test2.py        # read protocol.inf / keyprotocol.inf / sys.inf from the scope
    python Test3.py        # screenshot to PNG (legacy 8-bit)
    python Color-Sreenshot.py

Changes: `RemoteShell` was removed (it can brick the instrument), the constructor
raises instead of calling `exit()`, Test2 no longer uses the remote shell and
Test3 avoids ':' in file names (invalid on Windows). For new code use
`hantek_mcp` directly - see the top-level README.
