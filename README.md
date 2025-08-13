# ADAUSDT DCA BULLS Bot (Bybit)

A Bybit trading bot that follows the same strategy as your Pine script:
- 1H **BULLS** signal for entries (confirmed close)
- **DCA** (Long 11.05%, Short 8.61%), geometric sizing, up to 5 adds
- **TP 1%** on average entry (strict), optional net-profit USD floor
- **Flip** on *fresh* opposite 1H signal *if* current PnL% > buffer
- Optional **Emergency SL %** on average entry
- **Reseed immediately** after a close using the last known 1H direction (toggle)

## Deploy on Render

### 1) Create a **Background Worker** service
- Connect your repo.
- Use the included `render.yaml` (auto) or set manually:
  - **Build Command**: `pip install -r requirements.txt`
  - **Start Command**: `python ada_dca_bulls_bot.py`

### 2) Environment Variables
- `BYBIT_API_KEY`, `BYBIT_API_SECRET` (Render → Environment → Add Secret)
- Optional:
  - `SYMBOL=ADAUSDT`, `CATEGORY=linear`
  - `EQUITY_USDT=150`, `USE_CROSS=0|1`, `LEVERAGE_X=10`
  - `TP_PCT=1.0`, `MAX_DCA=5`
  - `VOL_SCALE_LONG=1.18`, `VOL_SCALE_SHORT=1.15`
  - `FIT_LONG_STEP=11.05`, `FIT_SHORT_STEP=8.61`
  - `FLIP_BUFFER_PCT=0.5`, `MIN_PROFIT_USD=0.00`
  - `USE_EMERGENCY_SL=0|1`, `EMERGENCY_SL_PCT=6.0`
  - `RESEED_IMMEDIATELY=1`, `SEED_FRESH_ONLY=0`
  - `LOG_LEVEL=INFO`, `POLL_SEC=3`

## Notes

- Orders: entries & DCA via **Market**, TP via **Limit PostOnly (maker)** reduceOnly.  
- Qty sizing: `qty = leg_usdt / price`, then **rounded to Bybit lot** & min-qty enforced (same approach as your DOGE bot).  
- Leverage is set via API, but liquidation is exchange-side — use at your own risk.  
- This bot **does not** guarantee profitability; test with **paper trading** or small size first.
