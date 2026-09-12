r"""
straddle_executor.py - runs the full straddle sequence for ONE MT5 terminal
against ONE scheduled macro event. Spawned as a subprocess by main.py, once
per detected terminal, so every open terminal trades in parallel.

Symbol resolution: brokers label gold differently (XAUUSD, XAUUSDc, XAUUSD.m,
etc). This script searches the terminal's own symbol list for anything
starting with config.SYMBOL and uses whatever it finds - no manual editing
needed per broker.

EXIT / DELETION LOGIC (updated after the Sep 11 CPI run):
  - If NEITHER side has filled at all by POST_RELEASE_NO_FILL_TIMEOUT_SECONDS
    after release, delete every pending order and stop - this is the
    "no real surprise happened" case.
  - Once ANY fill happens on EITHER side, the opposite side is left alone -
    it is NOT deleted anymore. This is deliberate: a fast initial move can
    reverse hard before the delete request completes (this happened during
    the Sep 11 CPI release - see CPI_logs.md), and by the time that
    reversal lands, the "opposite" side may already be filling for real.
    Both sides are allowed to be open simultaneously; each side's own SL
    is what cuts the wrong-direction exposure, not an early delete.
  - At exactly EXIT_SECONDS_AFTER_RELEASE seconds after release: close
    every open position (regardless of side or P&L) AND delete any orders
    still pending on either side at that moment. This is the one and only
    hard stop for the whole sequence.
  - Positions that disappear on their own before the hard exit (i.e. an SL
    was hit) are detected and logged with their actual close price/result,
    pulled from MT5's deal history - previously this only showed up as a
    position silently vanishing with no log line explaining why.

Standalone usage:
    python straddle_executor.py --terminal-path "C:\path\to\terminal64.exe" --event-name "CPI" --release-time-utc "2026-09-11T12:30:00+00:00"
"""
import argparse
import json
import time
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5
import config


def log(tag, msg):
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] [{tag}] {msg}")


def resolve_symbol(base_symbol, event_name):
    all_symbols = mt5.symbols_get()
    if not all_symbols:
        raise RuntimeError("No symbols returned by terminal - check MT5 connection.")

    candidates = [s.name for s in all_symbols if s.name.upper().startswith(base_symbol.upper())]
    if not candidates:
        raise RuntimeError(f"No symbol starting with '{base_symbol}' found on this terminal.")

    exact = [c for c in candidates if c.upper() == base_symbol.upper()]
    resolved = exact[0] if exact else sorted(candidates, key=len)[0]

    log(event_name, f"SYMBOL RESOLVED: '{base_symbol}' -> '{resolved}' (candidates seen: {candidates})")
    return resolved


def connect(terminal_path, event_name):
    if not mt5.initialize(path=terminal_path):
        raise RuntimeError(f"MT5 initialize() failed for {terminal_path}: {mt5.last_error()}")

    symbol = resolve_symbol(config.SYMBOL, event_name)

    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"Symbol {symbol} resolved but symbol_info() returned None.")
    if not info.visible:
        mt5.symbol_select(symbol, True)
        info = mt5.symbol_info(symbol)

    acct = mt5.account_info()
    log(event_name, f"CONNECTED. Account={acct.login if acct else 'unknown'} "
                     f"Broker={acct.company if acct else 'unknown'} Symbol={symbol} Point={info.point}")
    return info, symbol


def check_broker_time_offset(symbol, event_name):
    tick = mt5.symbol_info_tick(symbol)
    broker_time = datetime.fromtimestamp(tick.time, tz=timezone.utc)
    now_utc = datetime.now(timezone.utc)
    offset_hours = (broker_time - now_utc).total_seconds() / 3600
    log(event_name, f"DIAG: broker server time offset from true UTC: {offset_hours:+.2f}h")
    if abs(offset_hours) > 6:
        log(event_name, "DIAG WARNING: broker offset looks unusual - double check terminal clock.")


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
    while True:
        now = datetime.now(timezone.utc)
        remaining = (target_utc - now).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.05))


def append_execution_log(event_name, release_time_utc, side, entry_price, exit_price, outcome, slippage_points=None):
    record = {
        "event": event_name,
        "release_time_utc": release_time_utc.isoformat(),
        "closed_at_utc": datetime.now(timezone.utc).isoformat(),
        "side": side,
        "entry": entry_price,
        "exit": exit_price,
        "outcome": outcome,
        "slippage_points": slippage_points,
    }
    try:
        with open(config.EXECUTION_LOG_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        log(event_name, f"Failed to write execution log: {e}")


def place_straddle(symbol, symbol_info, event_name):
    point = symbol_info.point
    tick = mt5.symbol_info_tick(symbol)
    mid_price = (tick.ask + tick.bid) / 2
    filling = get_filling_mode(symbol_info)

    buy_stop_price = round(mid_price + config.STOP_DISTANCE_POINTS * point, symbol_info.digits)
    sell_stop_price = round(mid_price - config.STOP_DISTANCE_POINTS * point, symbol_info.digits)
    buy_sl = round(buy_stop_price - config.INITIAL_SL_POINTS * point, symbol_info.digits)
    sell_sl = round(sell_stop_price + config.INITIAL_SL_POINTS * point, symbol_info.digits)

    log(event_name, f"PLACING STRADDLE. Mid={mid_price} BuyStop(intended)={buy_stop_price} "
                     f"SellStop(intended)={sell_stop_price} SL_dist={config.INITIAL_SL_POINTS}pts")

    buy_tickets, sell_tickets = [], []

    for i in range(config.NUM_ORDERS_PER_SIDE):
        req = {
            "action": mt5.TRADE_ACTION_PENDING, "symbol": symbol, "volume": config.LOT_SIZE,
            "type": mt5.ORDER_TYPE_BUY_STOP, "price": buy_stop_price, "sl": buy_sl,
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling,
            "magic": config.MAGIC_NUMBER, "comment": f"straddle_buy_{event_name[:10]}",
        }
        result = mt5.order_send(req)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            buy_tickets.append(result.order)
            log(event_name, f"BUY STOP #{i+1} PLACED ticket={result.order} price={buy_stop_price}")
        else:
            log(event_name, f"BUY STOP #{i+1} FAILED retcode={result.retcode} comment={result.comment}")

    for i in range(config.NUM_ORDERS_PER_SIDE):
        req = {
            "action": mt5.TRADE_ACTION_PENDING, "symbol": symbol, "volume": config.LOT_SIZE,
            "type": mt5.ORDER_TYPE_SELL_STOP, "price": sell_stop_price, "sl": sell_sl,
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling,
            "magic": config.MAGIC_NUMBER, "comment": f"straddle_sell_{event_name[:10]}",
        }
        result = mt5.order_send(req)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            sell_tickets.append(result.order)
            log(event_name, f"SELL STOP #{i+1} PLACED ticket={result.order} price={sell_stop_price}")
        else:
            log(event_name, f"SELL STOP #{i+1} FAILED retcode={result.retcode} comment={result.comment}")

    log(event_name, f"STRADDLE PLACED: {len(buy_tickets)} buy stop(s), {len(sell_tickets)} sell stop(s)")
    return buy_tickets, sell_tickets, buy_stop_price, sell_stop_price


def get_still_pending(symbol, tickets):
    current = mt5.orders_get(symbol=symbol)
    current_ids = {o.ticket for o in current} if current else set()
    return [t for t in tickets if t in current_ids]


def delete_pending_orders(tickets, label, event_name):
    for ticket in tickets:
        result = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": ticket})
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            log(event_name, f"DELETED {label} order {ticket} (unfilled)")
        else:
            log(event_name, f"DELETE FAILED for {label} order {ticket}: retcode={result.retcode} "
                             f"(likely already filled/gone - not necessarily an error)")


def get_our_positions(symbol):
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return []
    return [p for p in positions if p.magic == config.MAGIC_NUMBER]


def log_new_fills(symbol, side_label, intended_price, point, event_name, release_time_utc, logged_tickets):
    """Logs each position on this side the FIRST time it's seen, with
    slippage vs the intended stop price. Safe to call every poll - only
    logs tickets not already in logged_tickets."""
    want_type = mt5.ORDER_TYPE_BUY if side_label == "buy" else mt5.ORDER_TYPE_SELL
    seconds_since_release = (datetime.now(timezone.utc) - release_time_utc).total_seconds()

    for pos in get_our_positions(symbol):
        if pos.type != want_type or pos.ticket in logged_tickets:
            continue
        actual = pos.price_open
        slippage_points = (actual - intended_price) / point if side_label == "buy" else (intended_price - actual) / point
        log(event_name, f"FILL DETECTED [{side_label.upper()}] ticket={pos.ticket} "
                         f"intended={intended_price} actual={actual} "
                         f"slippage={slippage_points:+.1f}pts "
                         f"t+{seconds_since_release:.2f}s since release")
        logged_tickets.add(pos.ticket)


def detect_external_closes(symbol, symbol_info, known_positions, event_name):
    """Detects positions that vanished since the last poll WITHOUT us having
    closed them ourselves (i.e. hit their SL). Logs the real close price and
    P&L pulled from MT5's own deal history, so an SL hit is no longer silent."""
    point = symbol_info.point
    current_positions = get_our_positions(symbol)
    current_tickets = {p.ticket for p in current_positions}

    for ticket, info in list(known_positions.items()):
        if ticket in current_tickets:
            continue
        deals = mt5.history_deals_get(position=ticket)
        close_deal = deals[-1] if deals else None
        if close_deal:
            exit_price = close_deal.price
            pnl_points = (exit_price - info["entry"]) if info["side"] == "buy" else (info["entry"] - exit_price)
            pnl_points /= point
            log(event_name, f"POSITION CLOSED EXTERNALLY (likely SL hit) ticket={ticket} "
                             f"side={info['side']} entry={info['entry']} exit={exit_price} result={pnl_points:+.1f}pts")
            append_execution_log(event_name, info["release_time_utc"], info["side"], info["entry"], exit_price, "sl_hit")
        else:
            log(event_name, f"POSITION CLOSED EXTERNALLY ticket={ticket} (could not fetch close deal from history)")
        del known_positions[ticket]

    for pos in current_positions:
        if pos.ticket not in known_positions:
            known_positions[pos.ticket] = {
                "side": "buy" if pos.type == mt5.ORDER_TYPE_BUY else "sell",
                "entry": pos.price_open,
                "release_time_utc": None,  # filled in by caller before first use
            }


def manage_breakeven(symbol, symbol_info, breakeven_done, event_name):
    point = symbol_info.point
    tick = mt5.symbol_info_tick(symbol)
    for pos in get_our_positions(symbol):
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
                "action": mt5.TRADE_ACTION_SLTP, "position": pos.ticket, "symbol": symbol,
                "sl": new_sl, "tp": pos.tp,
            })
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                log(event_name, f"BREAKEVEN SET position={pos.ticket} new_sl={new_sl} "
                                 f"(locked {config.BREAKEVEN_LOCK_POINTS}pts)")
                breakeven_done.add(pos.ticket)
            else:
                log(event_name, f"BREAKEVEN MODIFY FAILED position={pos.ticket} retcode={result.retcode}")


def close_all_positions(symbol, symbol_info, event_name, release_time_utc):
    positions = get_our_positions(symbol)
    if not positions:
        log(event_name, "HARD EXIT: no open positions to close.")
        return

    filling = get_filling_mode(symbol_info)
    tick = mt5.symbol_info_tick(symbol)

    for pos in positions:
        if pos.type == mt5.ORDER_TYPE_BUY:
            close_type, close_price, side = mt5.ORDER_TYPE_SELL, tick.bid, "buy"
        else:
            close_type, close_price, side = mt5.ORDER_TYPE_BUY, tick.ask, "sell"

        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": pos.volume,
            "type": close_type, "position": pos.ticket, "price": close_price,
            "type_filling": filling, "magic": config.MAGIC_NUMBER, "comment": "hard_exit_60s",
        }
        result = mt5.order_send(req)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            pnl_points = (close_price - pos.price_open) if side == "buy" else (pos.price_open - close_price)
            pnl_points /= symbol_info.point
            log(event_name, f"HARD EXIT CLOSED position={pos.ticket} side={side} entry={pos.price_open} "
                             f"exit={close_price} result={pnl_points:+.1f}pts")
            append_execution_log(event_name, release_time_utc, side, pos.price_open, close_price, "closed_60s")
        else:
            log(event_name, f"HARD EXIT CLOSE FAILED position={pos.ticket} retcode={result.retcode} {result.comment}")
            append_execution_log(event_name, release_time_utc, side, pos.price_open, None, f"close_failed_{result.retcode}")


def run(terminal_path, event_name, release_time_utc):
    symbol_info, symbol = connect(terminal_path, event_name)
    check_broker_time_offset(symbol, event_name)

    place_at_dt = release_time_utc - timedelta(seconds=config.PRE_RELEASE_LEAD_SECONDS)
    log(event_name, f"WAITING until {place_at_dt.isoformat()} to place straddle (release at {release_time_utc.isoformat()})")
    wait_until(place_at_dt)

    buy_tickets, sell_tickets, buy_stop_price, sell_stop_price = place_straddle(symbol, symbol_info, event_name)
    if not buy_tickets and not sell_tickets:
        log(event_name, "No orders placed successfully. Exiting.")
        mt5.shutdown()
        return

    point = symbol_info.point
    buy_logged, sell_logged = set(), set()
    breakeven_done = set()
    known_positions = {}
    hard_exit_done = False

    log(event_name, "MONITORING for fills...")
    while True:
        now = datetime.now(timezone.utc)
        seconds_since_release = (now - release_time_utc).total_seconds()

        # Log any newly-filled positions on either side (no deletion tied to this anymore)
        log_new_fills(symbol, "buy", buy_stop_price, point, event_name, release_time_utc, buy_logged)
        log_new_fills(symbol, "sell", sell_stop_price, point, event_name, release_time_utc, sell_logged)

        # Detect and log any position that closed on its own (SL hit) before hard exit
        for ticket in list(known_positions.keys()):
            if known_positions[ticket]["release_time_utc"] is None:
                known_positions[ticket]["release_time_utc"] = release_time_utc
        detect_external_closes(symbol, symbol_info, known_positions, event_name)
        for ticket in known_positions:
            if known_positions[ticket]["release_time_utc"] is None:
                known_positions[ticket]["release_time_utc"] = release_time_utc

        # Breakeven applies to whatever's open on either side
        if get_our_positions(symbol):
            manage_breakeven(symbol, symbol_info, breakeven_done, event_name)

        # NO-FILL CASE: nothing has EVER filled on either side by the timeout -> clean slate, done
        no_positions_yet = not buy_logged and not sell_logged
        if no_positions_yet and seconds_since_release > config.POST_RELEASE_NO_FILL_TIMEOUT_SECONDS:
            log(event_name, f"NO FILL after {config.POST_RELEASE_NO_FILL_TIMEOUT_SECONDS}s post-release. Deleting all.")
            delete_pending_orders(get_still_pending(symbol, buy_tickets), "buy", event_name)
            delete_pending_orders(get_still_pending(symbol, sell_tickets), "sell", event_name)
            append_execution_log(event_name, release_time_utc, "none", None, None, "no_fill")
            break

        # HARD EXIT: fixed 60s mark - close everything open, delete everything still pending, no exceptions
        if not hard_exit_done and seconds_since_release >= config.EXIT_SECONDS_AFTER_RELEASE:
            log(event_name, f"HARD EXIT TRIGGERED at t+{seconds_since_release:.2f}s since release.")
            close_all_positions(symbol, symbol_info, event_name, release_time_utc)

            remaining_buy = get_still_pending(symbol, buy_tickets)
            remaining_sell = get_still_pending(symbol, sell_tickets)
            if remaining_buy:
                log(event_name, f"Cleaning up {len(remaining_buy)} still-pending buy order(s) at hard exit.")
                delete_pending_orders(remaining_buy, "buy", event_name)
            if remaining_sell:
                log(event_name, f"Cleaning up {len(remaining_sell)} still-pending sell order(s) at hard exit.")
                delete_pending_orders(remaining_sell, "sell", event_name)

            hard_exit_done = True
            break

        time.sleep(config.POLL_INTERVAL_SECONDS)

    log(event_name, "DONE with this event on this terminal.")
    mt5.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--terminal-path", required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--release-time-utc", required=True)
    args = parser.parse_args()

    release_dt = datetime.fromisoformat(args.release_time_utc)
    run(args.terminal_path, args.event_name, release_dt)
