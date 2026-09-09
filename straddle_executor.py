"""
straddle_executor.py - runs the full straddle sequence for ONE MT5 terminal
against ONE scheduled macro event. Spawned as a subprocess by main.py, once
per detected terminal, so every open terminal trades in parallel.

Exit rule: ALL open positions are force-closed exactly EXIT_SECONDS_AFTER_RELEASE
seconds after the official release time, regardless of breakeven state or P&L.

Standalone usage:
    python straddle_executor.py --terminal-path "C:\...\terminal64.exe" \
        --event-name "CPI" --release-time-utc "2026-09-11T12:30:00+00:00"
"""
import argparse
import json
import time
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5
import config


def log(tag, msg):
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] [{tag}] {msg}")


def connect(terminal_path):
    if not mt5.initialize(path=terminal_path):
        raise RuntimeError(f"MT5 initialize() failed for {terminal_path}: {mt5.last_error()}")
    info = mt5.symbol_info(config.SYMBOL)
    if info is None:
        raise RuntimeError(f"Symbol {config.SYMBOL} not found on this terminal.")
    if not info.visible:
        mt5.symbol_select(config.SYMBOL, True)
    acct = mt5.account_info()
    log(terminal_path, f"Connected. Account: {acct.login if acct else 'unknown'}, point size: {info.point}")
    return info


def check_broker_time_offset(symbol):
    """Diagnostic only - does NOT drive scheduling. Scheduling uses this machine's
    system UTC clock (GitHub Actions runners are NTP-synced). This just logs a
    sanity-check so a broken broker clock is visible in the logs."""
    tick = mt5.symbol_info_tick(symbol)
    broker_time = datetime.fromtimestamp(tick.time, tz=timezone.utc)
    now_utc = datetime.now(timezone.utc)
    offset_hours = (broker_time - now_utc).total_seconds() / 3600
    log("diag", f"Broker server time offset from true UTC: {offset_hours:+.2f}h")
    if abs(offset_hours) > 6:
        log("diag", "WARNING: broker offset looks unusual - double check terminal clock.")


def get_filling_mode(symbol_info):
    SYMBOL_FILLING_FOK = 1
    SYMBOL_FILLING_IOC = 2
    mode = symbol_info.filling_mode
    if mode & SYMBOL_FILLING_IOC:
        return mt5.ORDER_FILLING_IOC
    if mode & SYMBOL_FILLING_FOK:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def wait_until(target_utc):
    """Busy-wait (short sleeps) until system UTC clock reaches target_utc."""
    while True:
        now = datetime.now(timezone.utc)
        remaining = (target_utc - now).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.05))


def append_execution_log(event_name, release_time_utc, side, entry_price, exit_price, outcome):
    """Appends one JSON line per fill/close/no-fill outcome to EXECUTION_LOG_PATH.
    The dashboard's /api/history endpoint reads this file directly."""
    record = {
        "event": event_name,
        "release_time_utc": release_time_utc.isoformat(),
        "closed_at_utc": datetime.now(timezone.utc).isoformat(),
        "side": side,
        "entry": entry_price,
        "exit": exit_price,
        "outcome": outcome,
    }
    try:
        with open(config.EXECUTION_LOG_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        log(event_name, f"Failed to write execution log: {e}")


def place_straddle(symbol_info, event_name):
    point = symbol_info.point
    tick = mt5.symbol_info_tick(config.SYMBOL)
    mid_price = (tick.ask + tick.bid) / 2
    filling = get_filling_mode(symbol_info)

    buy_stop_price = round(mid_price + config.STOP_DISTANCE_POINTS * point, symbol_info.digits)
    sell_stop_price = round(mid_price - config.STOP_DISTANCE_POINTS * point, symbol_info.digits)
    buy_sl = round(buy_stop_price - config.INITIAL_SL_POINTS * point, symbol_info.digits)
    sell_sl = round(sell_stop_price + config.INITIAL_SL_POINTS * point, symbol_info.digits)

    log(event_name, f"Placing straddle. Mid={mid_price} Buy stop={buy_stop_price} Sell stop={sell_stop_price}")

    buy_tickets, sell_tickets = [], []

    for i in range(config.NUM_ORDERS_PER_SIDE):
        req = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": config.SYMBOL,
            "volume": config.LOT_SIZE,
            "type": mt5.ORDER_TYPE_BUY_STOP,
            "price": buy_stop_price,
            "sl": buy_sl,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
            "magic": config.MAGIC_NUMBER,
            "comment": f"straddle_buy_{event_name[:10]}",
        }
        result = mt5.order_send(req)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            buy_tickets.append(result.order)
        else:
            log(event_name, f"BUY STOP #{i+1} failed: {result.retcode} {result.comment}")

    for i in range(config.NUM_ORDERS_PER_SIDE):
        req = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": config.SYMBOL,
            "volume": config.LOT_SIZE,
            "type": mt5.ORDER_TYPE_SELL_STOP,
            "price": sell_stop_price,
            "sl": sell_sl,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
            "magic": config.MAGIC_NUMBER,
            "comment": f"straddle_sell_{event_name[:10]}",
        }
        result = mt5.order_send(req)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            sell_tickets.append(result.order)
        else:
            log(event_name, f"SELL STOP #{i+1} failed: {result.retcode} {result.comment}")

    log(event_name, f"Placed {len(buy_tickets)} buy stop(s), {len(sell_tickets)} sell stop(s)")
    return buy_tickets, sell_tickets


def get_still_pending(tickets):
    current = mt5.orders_get(symbol=config.SYMBOL)
    current_ids = {o.ticket for o in current} if current else set()
    return [t for t in tickets if t in current_ids]


def delete_pending_orders(tickets, label, event_name):
    for ticket in tickets:
        result = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": ticket})
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            log(event_name, f"Deleted {label} order {ticket}")
        else:
            log(event_name, f"Could not delete {label} order {ticket}: {result.retcode}")


def get_our_positions():
    positions = mt5.positions_get(symbol=config.SYMBOL)
    if not positions:
        return []
    return [p for p in positions if p.magic == config.MAGIC_NUMBER]


def manage_breakeven(symbol_info, breakeven_done, event_name):
    point = symbol_info.point
    tick = mt5.symbol_info_tick(config.SYMBOL)
    for pos in get_our_positions():
        if pos.ticket in breakeven_done:
            continue
        if pos.type == mt5.ORDER_TYPE_BUY:
            profit_points = (tick.bid - pos.price_open) / point
            new_sl = round(pos.price_open + config.BREAKEVEN_LOCK_POINTS * point, symbol_info.digits)
        else:
            profit_points = (pos.price_open - tick.ask) / point
            new_sl = round(pos.price_open - config.BREAKEVEN_LOCK_POINTS * point, symbol_info.digits)

        if profit_points >= config.BREAKEVEN_TRIGGER_POINTS:
            result = mt5.order_send({
                "action": mt5.TRADE_ACTION_SLTP,
                "position": pos.ticket,
                "symbol": config.SYMBOL,
                "sl": new_sl,
                "tp": pos.tp,
            })
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                log(event_name, f"Breakeven SL set for position {pos.ticket}: {new_sl}")
                breakeven_done.add(pos.ticket)
            else:
                log(event_name, f"Breakeven modify failed for {pos.ticket}: {result.retcode}")


def close_all_positions(symbol_info, event_name, release_time_utc):
    """Force-close every open position from this straddle, market price, regardless
    of P&L. This is the hard 60-second exit rule - it overrides everything else."""
    positions = get_our_positions()
    if not positions:
        log(event_name, "Hard exit triggered - no open positions to close.")
        return

    filling = get_filling_mode(symbol_info)
    tick = mt5.symbol_info_tick(config.SYMBOL)

    for pos in positions:
        if pos.type == mt5.ORDER_TYPE_BUY:
            close_type, close_price, side = mt5.ORDER_TYPE_SELL, tick.bid, "buy"
        else:
            close_type, close_price, side = mt5.ORDER_TYPE_BUY, tick.ask, "sell"

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": config.SYMBOL,
            "volume": pos.volume,
            "type": close_type,
            "position": pos.ticket,
            "price": close_price,
            "type_filling": filling,
            "magic": config.MAGIC_NUMBER,
            "comment": "hard_exit_60s",
        }
        result = mt5.order_send(req)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            log(event_name, f"Closed position {pos.ticket} at {close_price} (hard 60s exit)")
            append_execution_log(event_name, release_time_utc, side, pos.price_open, close_price, "closed_60s")
        else:
            log(event_name, f"FAILED to close position {pos.ticket}: {result.retcode} {result.comment}")
            append_execution_log(event_name, release_time_utc, side, pos.price_open, None, f"close_failed_{result.retcode}")


def run(terminal_path, event_name, release_time_utc):
    symbol_info = connect(terminal_path)
    check_broker_time_offset(config.SYMBOL)

    place_at_dt = release_time_utc - timedelta(seconds=config.PRE_RELEASE_LEAD_SECONDS)
    log(event_name, f"Waiting until {place_at_dt.isoformat()} to place straddle "
                     f"(release at {release_time_utc.isoformat()})")
    wait_until(place_at_dt)

    buy_tickets, sell_tickets = place_straddle(symbol_info, event_name)
    if not buy_tickets and not sell_tickets:
        log(event_name, "No orders placed successfully. Exiting.")
        mt5.shutdown()
        return

    opposite_cleared = False
    breakeven_done = set()
    hard_exit_done = False

    log(event_name, "Monitoring for fills...")
    while True:
        now = datetime.now(timezone.utc)
        seconds_since_release = (now - release_time_utc).total_seconds()

        buy_still_pending = get_still_pending(buy_tickets)
        sell_still_pending = get_still_pending(sell_tickets)
        buy_filled = len(buy_still_pending) < len(buy_tickets)
        sell_filled = len(sell_still_pending) < len(sell_tickets)

        if not opposite_cleared and (buy_filled or sell_filled):
            if buy_filled and sell_still_pending:
                log(event_name, f"Buy side filled -> deleting {len(sell_still_pending)} sell stop(s)")
                delete_pending_orders(sell_still_pending, "sell", event_name)
            elif sell_filled and buy_still_pending:
                log(event_name, f"Sell side filled -> deleting {len(buy_still_pending)} buy stop(s)")
                delete_pending_orders(buy_still_pending, "buy", event_name)
            opposite_cleared = True

        if get_our_positions():
            manage_breakeven(symbol_info, breakeven_done, event_name)

        # No-fill timeout: nothing filled at all shortly after release -> clean up, log, done
        if not opposite_cleared and seconds_since_release > config.POST_RELEASE_NO_FILL_TIMEOUT_SECONDS:
            log(event_name, f"No fill {config.POST_RELEASE_NO_FILL_TIMEOUT_SECONDS}s after release. Deleting all.")
            delete_pending_orders(buy_still_pending, "buy", event_name)
            delete_pending_orders(sell_still_pending, "sell", event_name)
            append_execution_log(event_name, release_time_utc, "none", None, None, "no_fill")
            break

        # HARD EXIT: exactly EXIT_SECONDS_AFTER_RELEASE after release, close everything,
        # no exceptions - fires regardless of breakeven state or P&L
        if not hard_exit_done and seconds_since_release >= config.EXIT_SECONDS_AFTER_RELEASE:
            log(event_name, f"{config.EXIT_SECONDS_AFTER_RELEASE}s after release reached - hard exit.")
            close_all_positions(symbol_info, event_name, release_time_utc)
            hard_exit_done = True
            break

        time.sleep(config.POLL_INTERVAL_SECONDS)

    log(event_name, "Done with this event on this terminal.")
    mt5.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--terminal-path", required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--release-time-utc", required=True)
    args = parser.parse_args()

    release_dt = datetime.fromisoformat(args.release_time_utc)
    run(args.terminal_path, args.event_name, release_dt)
