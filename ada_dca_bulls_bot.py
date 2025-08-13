#!/usr/bin/env python3
"""
ADAUSDT DCA + BULLS(1H) Signal Bot for Bybit (Unified Trading API)

- Reuses Bybit connectivity & qty rounding patterns like user's DOGE bot:
  * market entries/DCA, maker limit TP (PostOnly, reduceOnly)
  * qty = leg_usdt / price, rounded by lot size; enforce min qty
  * leverage set via API; one-way mode

- Strategy matches Pine logic discussed:
  * Signals: BULLS on 1H (confirmed close). Seed when FLAT if signal present.
  * Reseed immediately after close using last known 1H direction (optional).
  * DCA steps: Long 11.05%, Short 8.61% (range-fit). Max DCA = 5. Geometric sizing.
  * TP: 1% over avg price (strict). Optional min profit USD to cover fees.
  * Flip: on *fresh* opposite 1H signal and pnl% > flip_buffer_pct.
  * Optional Emergency SL % (on avg price). (Default OFF.)

ENV (set on Render):
  BYBIT_API_KEY, BYBIT_API_SECRET
Optional:
  SYMBOL (default ADAUSDT), CATEGORY (default linear)
  LOG_LEVEL (INFO|WARNING), POLL_SEC (default 3)
  EQUITY_USDT (default 150), USE_CROSS (0/1, default 0), LEVERAGE_X (default 10)
  TP_PCT (default 1.0), MAX_DCA (default 5)
  VOL_SCALE_LONG (1.18), VOL_SCALE_SHORT (1.15)
  FIT_LONG_STEP (11.05), FIT_SHORT_STEP (8.61)
  FLIP_BUFFER_PCT (0.5)
  MIN_PROFIT_USD (0.00)  # set small positive to ensure net after fees
  USE_EMERGENCY_SL (0/1, default 0), EMERGENCY_SL_PCT (6.0)
  RESEED_IMMEDIATELY (0/1, default 1), SEED_FRESH_ONLY (0/1, default 0)
"""

import os, time, math, uuid, logging, sys
from dataclasses import dataclass, field
from typing import Optional, Tuple, List
from pybit.unified_trading import HTTP

# ---------------- logging ----------------
_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, _level, logging.INFO),
                    format="%(asctime)s %(levelname)s: %(message)s")

# --------------- config ------------------
def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return default

def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except Exception:
        return default

def env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name, None)
    if v is None: return default
    return str(v).strip().lower() in ["1","true","yes","on"]

SYMBOL   = os.environ.get("SYMBOL", "ADAUSDT")
CATEGORY = os.environ.get("CATEGORY", "linear")
POLL_SEC = env_float("POLL_SEC", 3.0)

EQUITY_USDT = env_float("EQUITY_USDT", 150.0)
USE_CROSS   = env_bool("USE_CROSS", False)
LEVERAGE_X  = env_float("LEVERAGE_X", 10.0)

TP_PCT      = env_float("TP_PCT", 1.0)               # %
MAX_DCA     = env_int("MAX_DCA", 5)

VOL_SCALE_LONG  = env_float("VOL_SCALE_LONG", 1.18)
VOL_SCALE_SHORT = env_float("VOL_SCALE_SHORT", 1.15)

FIT_LONG_STEP   = env_float("FIT_LONG_STEP", 11.05) / 100.0
FIT_SHORT_STEP  = env_float("FIT_SHORT_STEP", 8.61) / 100.0

FLIP_BUFFER_PCT = env_float("FLIP_BUFFER_PCT", 0.5)  # %
MIN_PROFIT_USD  = env_float("MIN_PROFIT_USD", 0.00)

USE_EMERGENCY_SL   = env_bool("USE_EMERGENCY_SL", False)
EMERGENCY_SL_PCT   = env_float("EMERGENCY_SL_PCT", 6.0)

RESEED_IMMEDIATELY = env_bool("RESEED_IMMEDIATELY", True)
SEED_FRESH_ONLY    = env_bool("SEED_FRESH_ONLY", False)

TAKER_FEE = env_float("TAKER_FEE", 0.0006)  # 0.06%
MAKER_FEE = env_float("MAKER_FEE", 0.0002)  # used for info logs only

# -------------- utils --------------------
def with_retry(fn, *args, **kwargs):
    tries = kwargs.pop("_tries", 5)
    for i in range(tries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            wait = 1.2 * (2 ** i)
            logging.warning("API call failed (%d/%d): %s; retrying in %.1fs", i+1, tries, e, wait)
            time.sleep(wait)
    return fn(*args, **kwargs)

# -------------- BULLS(1H) signal ----------------
def bulls_signal_from_klines(klines: List[List[str]]) -> Tuple[bool,bool,bool,bool]:
    """
    Input: Bybit get_kline result list (sorted ascending by start time).
    Returns: (sigL, sigS, freshL, freshS) on *last closed* 1H candle.
    Mirrors Pine logic:
      - bindex counts closes > close[4]
      - sindex counts closes < close[4]
      - condShort = bindex > bars and close < open and high >= highest(high, length)
      - condLong  = sindex > bars and close > open and low  <= lowest(low,  length)
      - Reset indexes when a signal prints
    """
    length = 50; bars = 30
    # Build OHLC arrays
    o = [float(x[1]) for x in klines]
    h = [float(x[2]) for x in klines]
    l = [float(x[3]) for x in klines]
    c = [float(x[4]) for x in klines]
    n = len(c)
    if n < max(length, 35):
        return (False, False, False, False)

    # Calculate rolling highest/lowest
    highest = [max(h[max(0, i-length+1):i+1]) for i in range(n)]
    lowest  = [min(l[max(0, i-length+1):i+1]) for i in range(n)]

    bindex = [0]*n; sindex = [0]*n
    for i in range(n):
        if i>=1:
            bindex[i] = bindex[i-1]
            sindex[i] = sindex[i-1]
        if i>=4 and c[i] > c[i-4]: bindex[i] += 1
        if i>=4 and c[i] < c[i-4]: sindex[i] += 1

        condShort = bindex[i] > bars and c[i] < o[i] and h[i] >= highest[i]
        condLong  = sindex[i] > bars and c[i] > o[i] and l[i] <= lowest[i]
        if condShort:
            bindex[i] = 0
        if condLong:
            sindex[i] = 0

    Lelex = [1 if (sindex[i] == 0 and c[i] > o[i] and l[i] <= lowest[i]) else
             (-1 if (bindex[i] == 0 and c[i] < o[i] and h[i] >= highest[i]) else 0) for i in range(n)]
    # On last closed candle
    sigL = (Lelex[-1] == 1); sigS = (Lelex[-1] == -1)
    freshL = sigL and (Lelex[-2] != 1)
    freshS = sigS and (Lelex[-2] != -1)
    return (sigL, sigS, freshL, freshS)

# -------------- core bot ----------------
class ADADcaBullsBot:
    def __init__(self, http: HTTP):
        self.http = http
        self.symbol = SYMBOL; self.category = CATEGORY
        self.poll_sec = POLL_SEC

        # exchange filters
        info = with_retry(self.http.get_instruments_info, category=self.category, symbol=self.symbol)
        inst = info["result"]["list"][0]
        lot = inst["lotSizeFilter"]; pricef = inst["priceFilter"]
        self.qty_step = float(lot["qtyStep"]); self.min_qty = float(lot["minOrderQty"])
        self.tick_size = float(pricef["tickSize"])

        # one-way & leverage
        try:
            self.http.switch_position_mode(category=self.category, symbol=self.symbol, mode=0)
            logging.info("Position mode One-Way")
        except Exception as e:
            if "not modified" in str(e): logging.info("Position mode unchanged (One-Way).")
            else: logging.warning("Cannot switch One-Way: %s", e)
        try:
            self.http.set_leverage(category=self.category, symbol=self.symbol,
                                   buyLeverage=str(LEVERAGE_X), sellLeverage=str(LEVERAGE_X))
            logging.info("Leverage set to %sx", LEVERAGE_X)
        except Exception as e:
            if "not modified" in str(e): logging.info("Leverage unchanged.")
            else: logging.warning("Cannot set leverage: %s", e)

        # state
        self.pos_qty = 0.0
        self.avg_entry: Optional[float] = None
        self.used_usdt = 0.0
        self.leg_usdt = 0.0
        self.level = -1
        self.last_fill_px: Optional[float] = None
        self.entry_fees_paid = 0.0
        self.tp_order_id: Optional[str] = None
        self.last_dir = 0  # 1=Long, -1=Short
        self.prev_sigL = False; self.prev_sigS = False

        # budget & base sizing
        self.budget = EQUITY_USDT * (LEVERAGE_X if USE_CROSS else 1.0)
        self.long_base, self.short_base = self._calc_bases()

    # ----- sizing helpers -----
    def _sum_geo(self, scale: float, k: int) -> float:
        return (1.0 - (scale**k)) / (1.0 - scale) if k>0 else 0.0

    def _calc_bases(self) -> Tuple[float,float]:
        long_total_mult  = self._sum_geo(VOL_SCALE_LONG,  1 + MAX_DCA)
        short_total_mult = self._sum_geo(VOL_SCALE_SHORT, 1 + MAX_DCA)
        return self.budget/long_total_mult, self.budget/short_total_mult

    def _round_qty(self, q: float) -> float:
        q = math.floor(q / self.qty_step) * self.qty_step
        if 0 < q < self.min_qty: q = self.min_qty
        return q

    def _round_px(self, px: float) -> float:
        return math.floor(px / self.tick_size) * self.tick_size

    def _last_and_mark(self) -> Tuple[float,float]:
        r = with_retry(self.http.get_tickers, category=self.category, symbol=self.symbol)
        item = r["result"]["list"][0]
        last = float(item["lastPrice"]); mark = float(item.get("markPrice", last))
        return last, mark

    def _mkt(self, side: str, qty: float, price: float, reduce: bool=False):
        qty = self._round_qty(qty)
        if qty <= 0: return
        notional = qty * price
        link = str(uuid.uuid4())
        with_retry(self.http.place_order, category=self.category, symbol=self.symbol,
                   side="Buy" if side=="long" else "Sell",
                   orderType="Market", qty=str(qty), reduceOnly=reduce, orderLinkId=link)
        if not reduce:
            # entry fees accumulate; exit fees accounted at close time
            self.entry_fees_paid += notional * TAKER_FEE
        logging.info("%s %s qty=%s", "OPEN" if not reduce else "CLOSE", side.upper(), qty)

    def _place_tp_limit(self):
        if self.pos_qty <= 0 or self.avg_entry is None: return
        trg = self._tp_target()
        if trg is None: return
        qty = self._round_qty(abs(self.pos_qty))
        self._cancel_tp()
        link = str(uuid.uuid4())
        side = "Sell" if self.pos_qty > 0 else "Buy"
        order = with_retry(self.http.place_order,
                           category=self.category, symbol=self.symbol,
                           side=side, orderType="Limit", qty=str(qty),
                           price=str(trg), reduceOnly=True, timeInForce="PostOnly",
                           closeOnTrigger=False, orderLinkId=link)
        self.tp_order_id = order.get("result", {}).get("orderId")
        logging.info("Place TP %s at %.6f qty=%s id=%s", side, trg, qty, self.tp_order_id)

    def _cancel_tp(self):
        if not self.tp_order_id: return
        try:
            with_retry(self.http.cancel_order, category=self.category, symbol=self.symbol, orderId=self.tp_order_id)
            logging.info("Cancelled TP %s", self.tp_order_id)
        except Exception as e:
            logging.warning("Cancel TP failed: %s", e)
        finally:
            self.tp_order_id = None

    def _tp_target(self) -> Optional[float]:
        if self.pos_qty == 0 or self.avg_entry is None: return None
        if self.pos_qty > 0:
            # Long TP: exact 1% OR minimal net profit USD (if set)
            raw = self.avg_entry * (1.0 + TP_PCT/100.0)
            if MIN_PROFIT_USD > 0:
                # ensure net >= MIN_PROFIT_USD
                qty = abs(self.pos_qty)
                rhs = (self.entry_fees_paid + MIN_PROFIT_USD) / max(qty,1e-9)
                raw = max(raw, (self.avg_entry + rhs)/(1.0 - TAKER_FEE))
            return self._round_px(raw)
        else:
            raw = self.avg_entry * (1.0 - TP_PCT/100.0)
            if MIN_PROFIT_USD > 0:
                qty = abs(self.pos_qty)
                rhs = (self.entry_fees_paid + MIN_PROFIT_USD) / max(qty,1e-9)
                raw = min(raw, (self.avg_entry - rhs)/(1.0 + TAKER_FEE))
            return self._round_px(raw)

    def _expected_pnl_pct(self, price: float) -> float:
        if self.pos_qty == 0 or self.avg_entry is None: return 0.0
        if self.pos_qty > 0:
            return 100.0 * (price/self.avg_entry - 1.0)
        else:
            return 100.0 * (1.0 - price/self.avg_entry)

    def _reset_state(self):
        self.pos_qty = 0.0; self.avg_entry = None; self.used_usdt = 0.0
        self.leg_usdt = 0.0; self.level = -1; self.last_fill_px = None
        self.entry_fees_paid = 0.0
        self._cancel_tp()

    # ----- signal pulling & seeding -----
    def _pull_bulls_1h(self) -> Tuple[bool,bool,bool,bool]:
        r = with_retry(self.http.get_kline, category=self.category, symbol=self.symbol, interval="60", limit=200)
        lst = sorted(r["result"]["list"], key=lambda x: int(x[0]))
        # drop the very last if it's not closed? Bybit returns closed klines for historical "list"
        return bulls_signal_from_klines(lst)

    def _seed_if_flat(self, price: float, sigL: bool, sigS: bool, freshL: bool, freshS: bool):
        flat = (self.pos_qty == 0.0)
        if not flat: return
        # choose seed mode
        seedL = freshL if SEED_FRESH_ONLY else sigL
        seedS = freshS if SEED_FRESH_ONLY else sigS
        if seedL:
            usdt = self.long_base; qty = self._round_qty(usdt / price)
            self._mkt("long", qty, price, reduce=False)
            self.pos_qty += qty; self.avg_entry = price
            self.used_usdt = usdt; self.leg_usdt = usdt; self.level = 0; self.last_fill_px = price
            self.last_dir = 1
            self._place_tp_limit()
        elif seedS:
            usdt = self.short_base; qty = self._round_qty(usdt / price)
            self._mkt("short", qty, price, reduce=False)
            self.pos_qty -= qty; self.avg_entry = price
            self.used_usdt = usdt; self.leg_usdt = usdt; self.level = 0; self.last_fill_px = price
            self.last_dir = -1
            self._place_tp_limit()

    # ----- DCA -----
    def _next_adverse(self, is_long: bool, from_px: float, level: int) -> float:
        step = FIT_LONG_STEP if is_long else FIT_SHORT_STEP
        return from_px * (1.0 - step) if is_long else from_px * (1.0 + step)

    def _maybe_dca(self, price: float):
        if self.pos_qty == 0 or self.level < 0 or self.level >= MAX_DCA: return
        is_long = self.pos_qty > 0
        next_px = self._next_adverse(is_long, self.last_fill_px, self.level)
        if is_long and price <= next_px:
            next_leg = self.leg_usdt * VOL_SCALE_LONG
            if self.used_usdt + next_leg <= self.budget + 1e-6:
                add_qty = self._round_qty(next_leg / price)
                if add_qty > 0:
                    prev_qty = self.pos_qty
                    self._mkt("long", add_qty, price, reduce=False)
                    new_qty = prev_qty + add_qty
                    self.avg_entry = ((self.avg_entry*prev_qty) + price*add_qty) / new_qty
                    self.pos_qty = new_qty
                    self.level += 1; self.leg_usdt = next_leg; self.used_usdt += next_leg
                    self.last_fill_px = price
                    self._place_tp_limit()
        elif (not is_long) and price >= next_px:
            next_leg = self.leg_usdt * VOL_SCALE_SHORT
            if self.used_usdt + next_leg <= self.budget + 1e-6:
                add_qty = self._round_qty(next_leg / price)
                if add_qty > 0:
                    prev_qty = abs(self.pos_qty)
                    self._mkt("short", add_qty, price, reduce=False)
                    new_qty = prev_qty + add_qty
                    self.avg_entry = ((self.avg_entry*prev_qty) + price*add_qty) / new_qty
                    self.pos_qty = -new_qty
                    self.level += 1; self.leg_usdt = next_leg; self.used_usdt += next_leg
                    self.last_fill_px = price
                    self._place_tp_limit()

    # ----- exits: TP, SL, flip -----
    def _check_tp_filled_by_sync(self):
        try:
            r = with_retry(self.http.get_positions, category=self.category, symbol=self.symbol)
            lst = r.get("result", {}).get("list", [])
            size = 0.0
            for p in lst:
                sz = float(p.get("size") or 0.0)
                if sz > 0:
                    size = sz; break
            if size == 0.0 and self.pos_qty != 0.0:
                logging.info("Detected position closed on exchange (likely TP filled).")
                # reseed immediately if enabled
                price, mark = self._last_and_mark()
                self._reset_state()
                if RESEED_IMMEDIATELY and self.last_dir != 0:
                    usdt = self.long_base if self.last_dir==1 else self.short_base
                    qty = self._round_qty(usdt / price)
                    side = "long" if self.last_dir==1 else "short"
                    self._mkt(side, qty, price, reduce=False)
                    self.pos_qty = qty if side=="long" else -qty
                    self.avg_entry = price; self.used_usdt = usdt; self.leg_usdt = usdt
                    self.level = 0; self.last_fill_px = price
                    self._place_tp_limit()
        except Exception as e:
            logging.warning("sync pos failed: %s", e)

    def _maybe_emergency_sl(self, price: float):
        if not USE_EMERGENCY_SL or self.pos_qty == 0 or self.avg_entry is None: return
        if self.pos_qty > 0:
            sl = self.avg_entry * (1.0 - EMERGENCY_SL_PCT/100.0)
            if price <= sl:
                qty = self._round_qty(self.pos_qty)
                self._mkt("long", qty, price, reduce=True)
                logging.info("Emergency SL LONG at %.6f", price)
                self._reset_state()
        else:
            sl = self.avg_entry * (1.0 + EMERGENCY_SL_PCT/100.0)
            if price >= sl:
                qty = self._round_qty(abs(self.pos_qty))
                self._mkt("short", qty, price, reduce=True)
                logging.info("Emergency SL SHORT at %.6f", price)
                self._reset_state()

    def _maybe_flip_on_signal_profit(self, freshL: bool, freshS: bool, price: float):
        if self.pos_qty == 0 or self.avg_entry is None: return
        pnl_pct = self._expected_pnl_pct(price)
        if self.pos_qty > 0 and freshS and pnl_pct > FLIP_BUFFER_PCT:
            qty = self._round_qty(self.pos_qty)
            self._mkt("long", qty, price, reduce=True)
            self._reset_state()
            # open short immediately
            usdt = self.short_base; q = self._round_qty(usdt/price)
            self._mkt("short", q, price, reduce=False)
            self.pos_qty = -q; self.avg_entry = price
            self.used_usdt = usdt; self.leg_usdt = usdt; self.level = 0; self.last_fill_px = price
            self.last_dir = -1
            self._place_tp_limit()
        elif self.pos_qty < 0 and freshL and pnl_pct > FLIP_BUFFER_PCT:
            qty = self._round_qty(abs(self.pos_qty))
            self._mkt("short", qty, price, reduce=True)
            self._reset_state()
            # open long immediately
            usdt = self.long_base; q = self._round_qty(usdt/price)
            self._mkt("long", q, price, reduce=False)
            self.pos_qty = q; self.avg_entry = price
            self.used_usdt = usdt; self.leg_usdt = usdt; self.level = 0; self.last_fill_px = price
            self.last_dir = 1
            self._place_tp_limit()

    # ----- main loop -----
    def loop(self):
        logging.info("Bot started: %s %s | Budget=%.2f (Equity=%s, Cross=%s x%s)",
                     self.symbol, self.category, self.budget, EQUITY_USDT, USE_CROSS, LEVERAGE_X)
        while True:
            try:
                last, mark = self._last_and_mark()
                price = (last + mark)/2.0
            except Exception as e:
                logging.warning("Price fetch failed: %s", e); time.sleep(POLL_SEC); continue

            # TP sync
            self._check_tp_filled_by_sync()

            # Signals
            try:
                sigL, sigS, freshL, freshS = self._pull_bulls_1h()
                if sigL: self.last_dir = 1
                elif sigS: self.last_dir = -1
            except Exception as e:
                logging.warning("Signal fetch failed: %s", e)
                sigL=sigS=freshL=freshS=False

            # Seed when flat
            self._seed_if_flat(price, sigL, sigS, freshL, freshS)

            # DCA
            self._maybe_dca(price)

            # Flip on profit with fresh opposite
            self._maybe_flip_on_signal_profit(freshL, freshS, price)

            # Emergency SL (optional)
            self._maybe_emergency_sl(price)

            time.sleep(POLL_SEC)

# ------------------- main -------------------
def main():
    key = os.environ.get("BYBIT_API_KEY") or os.environ.get("API_KEY")
    sec = os.environ.get("BYBIT_API_SECRET") or os.environ.get("API_SECRET")
    if not key or not sec:
        raise SystemExit("Set BYBIT_API_KEY/BYBIT_API_SECRET (or API_KEY/API_SECRET)")
    http = HTTP(api_key=key, api_secret=sec, recv_window=60000)
    bot = ADADcaBullsBot(http)
    bot.loop()

if __name__ == "__main__":
    main()
