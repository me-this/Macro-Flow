"""
api_server.py - lightweight HTTP API for the dashboard. Started as a
background thread from main.py so it's alive for the entire RDP session,
independent of whether any macro event is currently scheduled.

Endpoints:
  GET /api/terminals - detected MT5 terminals + live balance/equity
  GET /api/schedule  - upcoming macro events with countdown
  GET /api/history   - past executed events (read from EXECUTION_LOG_PATH)
"""
import json
import os
import threading
import time
from datetime import datetime, timezone
import os
from flask import send_from_directory

import MetaTrader5 as mt5
from flask import Flask, jsonify
from flask_cors import CORS

import config
from terminal_detector import find_mt5_terminals

app = Flask(__name__)
CORS(app)

_state_lock = threading.Lock()
_terminal_state = []


def _poll_terminals_loop():
    """Sequentially connects to each detected terminal, reads balance/equity,
    disconnects, moves to the next. Safe to run in this process because
    main.py's own process never holds an MT5 connection itself - the actual
    trading logic runs in separate straddle_executor.py subprocesses."""
    global _terminal_state
    while True:
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
        time.sleep(15)  # balances don't need faster polling than this


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

@app.route("/")
def dashboard():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")

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
