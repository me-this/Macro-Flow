"""
api_server.py - lightweight HTTP API for the dashboard.

Includes a "quiet window" around every scheduled macro event: MT5
polling pauses from QUIET_WINDOW_BEFORE_SECONDS before a release to
QUIET_WINDOW_AFTER_SECONDS after, so this background thread never
contends with straddle_executor.py for the same terminal connection
during the precision-critical window (this was the leading suspect
for a 2+ second setup delay observed during the Sep 16 FOMC run).
"""
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5
from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS

import config
from terminal_detector import find_mt5_terminals

app = Flask(__name__)
CORS(app)

_state_lock = threading.Lock()
_terminal_state = []
_was_in_quiet_window = False


def _in_quiet_window():
    now_utc = datetime.now(timezone.utc)
    for evt in config.MACRO_SCHEDULE_2026:
        release_utc = evt["datetime_et"].astimezone(timezone.utc)
        window_start = release_utc - timedelta(seconds=config.QUIET_WINDOW_BEFORE_SECONDS)
        window_end = release_utc + timedelta(seconds=config.QUIET_WINDOW_AFTER_SECONDS)
        if window_start <= now_utc <= window_end:
            return True, evt["event"]
    return False, None


def _poll_terminals_loop():
    global _terminal_state, _was_in_quiet_window
    while True:
        quiet, event_name = _in_quiet_window()

        if quiet:
            if not _was_in_quiet_window:
                print(f"[api_server] Entering quiet window for {event_name} - "
                      f"pausing MT5 dashboard polling.")
            _was_in_quiet_window = True
            time.sleep(2)  # check frequently so we resume promptly once the window ends
            continue

        if _was_in_quiet_window:
            print("[api_server] Quiet window ended - resuming MT5 dashboard polling.")
        _was_in_quiet_window = False

        results = []
        for path in find_mt5_terminals():
            entry = {"path": path, "connected": False}
            try:
                if mt5.initialize(path=path):
                    acct = mt5.account_info()
                    if acct:
                        positions = mt5.positions_get(symbol=config.SYMBOL) or []
                        entry.update({
                            "connected": True,
                            "account": acct.login,
                            "broker": acct.company,
                            "balance": acct.balance,
                            "equity": acct.equity,
                            "open_positions": len(positions),
                        })
                    mt5.shutdown()
            except Exception as e:
                entry["error"] = str(e)
            results.append(entry)

        with _state_lock:
            _terminal_state = results
        time.sleep(15)


@app.route("/")
def dashboard():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")


@app.route("/api/terminals")
def get_terminals():
    with _state_lock:
        return jsonify(_terminal_state)


@app.route("/api/schedule")
def get_schedule():
    now_utc = datetime.now(timezone.utc)
    upcoming = []
    for evt in config.MACRO_SCHEDULE_2026:
        release_utc = evt["datetime_et"].astimezone(timezone.utc)
        if release_utc > now_utc:
            upcoming.append({
                "event": evt["event"],
                "release_time_utc": release_utc.isoformat(),
                "seconds_until": (release_utc - now_utc).total_seconds(),
            })
    upcoming.sort(key=lambda e: e["seconds_until"])
    return jsonify(upcoming)


@app.route("/api/history")
def get_history():
    if not os.path.exists(config.EXECUTION_LOG_PATH):
        return jsonify([])
    records = []
    with open(config.EXECUTION_LOG_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    records.sort(key=lambda r: r.get("closed_at_utc", ""), reverse=True)
    return jsonify(records)


def run_api_server():
    threading.Thread(target=_poll_terminals_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=config.DASHBOARD_API_PORT, use_reloader=False)
