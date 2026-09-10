"""
tunnel.py - downloads bore.exe if not already present, launches it as a
background subprocess pointed at the dashboard API port, and parses the
public bore.pub URL straight out of its own startup output.

No manual PowerShell steps needed - main.py calls ensure_bore_binary()
then start_tunnel() at startup and prints the URL immediately.
"""
import os
import re
import subprocess
import threading
import urllib.request
import zipfile

BORE_ZIP_URL = "https://github.com/ekzhang/bore/releases/download/v0.6.0/bore-v0.6.0-x86_64-pc-windows-msvc.zip"
BORE_EXE_NAME = "bore.exe"
BORE_ZIP_NAME = "bore_download.zip"


def ensure_bore_binary():
    """Downloads and unzips bore.exe into the current folder if it isn't
    already there. Safe to call every run - skips the download if present."""
    if os.path.exists(BORE_EXE_NAME):
        print("[tunnel] bore.exe already present, skipping download.")
        return BORE_EXE_NAME

    print("[tunnel] Downloading bore.exe...")
    urllib.request.urlretrieve(BORE_ZIP_URL, BORE_ZIP_NAME)

    with zipfile.ZipFile(BORE_ZIP_NAME, "r") as z:
        z.extractall(".")

    os.remove(BORE_ZIP_NAME)

    if not os.path.exists(BORE_EXE_NAME):
        raise RuntimeError("bore.exe not found after extracting - the release archive layout may have changed.")

    print("[tunnel] bore.exe ready.")
    return BORE_EXE_NAME


def start_tunnel(local_port, timeout_seconds=20):
    """Launches bore.exe pointed at local_port, reads its stdout until the
    public URL appears, and returns (process, url). Keeps draining stdout
    in a background thread afterward so the subprocess never blocks on a
    full output buffer during the session."""
    exe_path = ensure_bore_binary()

    process = subprocess.Popen(
        [exe_path, "local", str(local_port), "--to", "bore.pub"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    url_pattern = re.compile(r"(bore\.pub:\d+)")
    found_url = {"value": None}

    def drain_output():
        for line in process.stdout:
            print(f"[bore] {line.rstrip()}")
            if found_url["value"] is None:
                match = url_pattern.search(line)
                if match:
                    found_url["value"] = f"http://{match.group(1)}"

    reader_thread = threading.Thread(target=drain_output, daemon=True)
    reader_thread.start()
    reader_thread.join(timeout=timeout_seconds)

    if found_url["value"] is None:
        raise RuntimeError(
            f"Could not detect bore's public URL within {timeout_seconds}s. "
            "Check the [bore] log lines above for errors."
        )

    return process, found_url["value"]
