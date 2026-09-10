r"""
terminal_detector.py - find all running MT5 terminal instances on this Windows machine.

CAVEAT: the MetaTrader5 Python package connects to a terminal via its executable
PATH (mt5.initialize(path=...)). This only reliably targets separate terminals if
each one is a SEPARATE installation folder (e.g. C:\MT5-FBS\terminal64.exe and
C:\MT5-Exness\terminal64.exe). Two terminal64.exe processes running from the exact
same install folder can't be told apart this way - so multi-terminal support here
assumes one install folder per broker/account, which is the normal setup anyway.
"""
import psutil

TERMINAL_PROCESS_NAMES = {"terminal64.exe", "terminal.exe"}


def find_mt5_terminals():
    """Return a list of unique executable paths for all running MT5 terminal processes."""
    found_paths = set()
    for proc in psutil.process_iter(["name", "exe"]):
        try:
            name = proc.info["name"]
            exe = proc.info["exe"]
            if name and name.lower() in TERMINAL_PROCESS_NAMES and exe:
                found_paths.add(exe)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return sorted(found_paths)
