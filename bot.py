#!/usr/bin/env python3
"""
MON Paper Trading Bot — RSI(40) × WMA(15) Crossover
Data : CoinGecko free public API (no key needed)
TF   : 3-minute synthetic candles (polled every 30s)
Acct : 100 USDC paper | Commission 0.08% round-trip
SL   : Below crossover candle low
TP   : Entry + risk × 2.2
Alert: Telegram on open & close
"""

# ── Auto-install dependencies ─────────────────────────────────────────────────
import subprocess, sys

def _install(pkg):
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet", "--break-system-packages", pkg],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

for _pkg in ("requests", "python-dotenv"):
    try:
        __import__(_pkg.replace("-", "_").split("[")[0])
    except ImportError:
        print(f"[bootstrap] installing {_pkg}...", flush=True)
        _install(_pkg)

# ── Stdlib + third-party ──────────────────────────────────────────────────────
import os, time, json, logging, threading
from datetime import datetime, timezone
from pathlib import Path
from collections import deque
import requests
from dotenv import load_dotenv

load_dotenv()

# ─── CONFIG ──────────────────────────────────────────────────────────────────
ACCOUNT_BALANCE  = 100.0
COMMISSION_RT    = 0.0008
RSI_PERIOD       = 40
WMA_PERIOD       = 15
TP_MULTIPLIER    = 2.2
CANDLE_SECONDS   = 180        # 3-minute candles
POLL_SECONDS     = 30         # price poll interval
MIN_CANDLES      = RSI_PERIOD + WMA_PERIOD + 10
STATS_FILE       = Path("/app/stats.json")
LOG_FILE         = Path("/app/bot.log")

CG_COIN_ID       = "monad"
CG_BASE          = "https://api.coingecko.com/api/v3"

TG_TOKEN = os.getenv("TG_BOT_TOKEN", "8349229275:AAGNWV2A0_Pf9LhlwZCczeBoMcUaJL2shFg")
TG_CHAT  = os.getenv("TG_CHAT_ID",   "1950462171")

# ─── LOGGING ─────────────────────────────────────────────────────────────────
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("MON-BOT")

# ─── STATS ───────────────────────────────────────────────────────────────────
def load_stats():
    if STATS_FILE.exists():
        try:
            return json.loads(STATS_FILE.read_text())
        except Exception:
            pass
    return {"wins": 0, "losses": 0, "pnl_usdc": 0.0, "trades": []}

def save_stats(s):
    STATS_FILE.write_text(json.dumps(s, indent=2))

# ─── TELEGRAM ────────────────────────────────────────────────────────────────
def tg_send(msg):
    if not TG_TOKEN or not TG_CHAT:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
        if not r.ok:
            log.warning(f"TG failed: {r.status_code} {r.text[:120]}")
    except Exception as e:
        log.warning(f"TG error: {e}")

# ─── COINGECKO PRICE FEED ────────────────────────────────────────────────────
_sess = requests.Session()
_sess.headers["User-Agent"] = "MON-PaperBot/1.0"

def fetch_price_usd():
    try:
        r = _sess.get(
            f"{CG_BASE}/simple/price",
            params={"ids": CG_COIN_ID, "vs_currencies": "usd"},
            timeout=10,
        )
        r.raise_for_status()
        return float(r.json()[CG_COIN_ID]["usd"])
    except Exception as e:
        log.warning(f"Price fetch error: {e}")
        return None

def fetch_historical_candles():
    """Pull 1 day of minute data from CoinGecko, resample to 3m candles."""
    try:
        r = _sess.get(
            f"{CG_BASE}/coins/{CG_COIN_ID}/market_chart",
            params={"vs_currency": "usd", "days": 1, "interval": "minutely"},
            timeout=20,
        )
        r.raise_for_status()
        prices = r.json().get("prices", [])
        if not prices:
            return []

        candles = []
        bms = CANDLE_SECONDS * 1000
        i = 0
        while i < len(prices):
            ts = prices[i][0]
            bucket_start = (ts // bms) * bms
            bp = []
            while i < len(prices) and prices[i][0] < bucket_start + bms:
                bp.append(prices[i][1])
                i += 1
            if bp:
                candles.append({
                    "ts":     bucket_start,
                    "open":   bp[0],
                    "high":   max(bp),
                    "low":    min(bp),
                    "close":  bp[-1],
                    "closed": True,
                })
        if candles:
            candles.pop()   # drop last (possibly incomplete) bucket
        log.info(f"Warm-up: {len(candles)} historical 3m candles loaded")
        return candles
    except Exception as e:
        log.warning(f"Historical fetch error: {e}")
        return []

# ─── CANDLE BUILDER ──────────────────────────────────────────────────────────
class CandleBuilder:
    def __init__(self):
        self._o = self._h = self._l = self._c = self._ts = None
        self._closed = deque(maxlen=300)

    def _bucket(self, now_ms):
        bms = CANDLE_SECONDS * 1000
        return (now_ms // bms) * bms

    def push(self, price):
        now_ms = int(time.time() * 1000)
        bucket = self._bucket(now_ms)

        if self._ts is None:
            self._ts = bucket
            self._o = self._h = self._l = self._c = price
            return None

        if bucket == self._ts:
            self._h = max(self._h, price)
            self._l = min(self._l, price)
            self._c = price
            return None

        # Candle closed — seal it
        closed = {"ts": self._ts, "open": self._o, "high": self._h,
                  "low": self._l, "close": self._c, "closed": True}
        self._closed.append(closed)
        self._ts = bucket
        self._o = self._h = self._l = self._c = price
        return closed

    def live_candle(self):
        if self._ts is None:
            return None
        return {"ts": self._ts, "open": self._o, "high": self._h,
                "low": self._l, "close": self._c, "closed": False}

    def closed_candles(self):
        return list(self._closed)

# ─── INDICATORS ──────────────────────────────────────────────────────────────
def calc_wma(closes, period):
    weights = list(range(1, period + 1))
    denom   = sum(weights)
    result  = []
    for i in range(len(closes)):
        if i < period - 1:
            result.append(None)
        else:
            w = closes[i - period + 1 : i + 1]
            result.append(sum(wt * v for wt, v in zip(weights, w)) / denom)
    return result

def calc_rsi(closes, period):
    rsi = [None] * period
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    if len(gains) < period:
        return [None] * len(closes)
    ag = sum(gains[:period])  / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i])  / period
        al = (al * (period - 1) + losses[i]) / period
        rsi.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    return rsi

def detect_crossover(rsi, wma):
    pairs = [(r, w) for r, w in zip(rsi, wma) if r is not None and w is not None]
    if len(pairs) < 2:
        return None
    pr, pw = pairs[-2]
    cr, cw = pairs[-1]
    if pr <= pw and cr > cw:
        return "bull"
    if pr >= pw and cr < cw:
        return "bear"
    return None

# ─── POSITION ────────────────────────────────────────────────────────────────
class Position:
    def __init__(self, entry, sl, tp, size_usdc):
        self.entry     = entry
        self.sl        = sl
        self.tp        = tp
        self.size_usdc = size_usdc
        self.opened_at = datetime.now(timezone.utc)

    def check_exit(self, candle):
        if candle["low"]  <= self.sl: return "SL"
        if candle["high"] >= self.tp: return "TP"
        return None

    def pnl(self, exit_price):
        gross = (exit_price - self.entry) / self.entry * self.size_usdc
        return gross - self.size_usdc * COMMISSION_RT

# ─── BOT ─────────────────────────────────────────────────────────────────────
class TradingBot:
    def __init__(self):
        self.stats    = load_stats()
        self.position = None
        self.builder  = CandleBuilder()
        self._lock    = threading.Lock()
        self._last_signal_ts = 0

    def _warmup(self):
        log.info("Fetching historical candles from CoinGecko...")
        for c in fetch_historical_candles():
            self.builder._closed.append(c)

    def run(self):
        log.info("=" * 60)
        log.info("  MON PAPER BOT  |  RSI(40) x WMA(15)  |  3-minute TF")
        log.info(f"  Paper account: {ACCOUNT_BALANCE} USDC  |  Feed: CoinGecko")
        log.info("=" * 60)

        self._warmup()

        tg_send(
            "*MON Paper Bot Started* \U0001f916\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"Strategy : RSI({RSI_PERIOD}) x WMA({WMA_PERIOD})\n"
            f"Timeframe : 3-minute\n"
            f"Account  : `{ACCOUNT_BALANCE} USDC` (paper)\n"
            f"Feed     : CoinGecko / MON-USD\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"W:{self.stats['wins']} L:{self.stats['losses']} | "
            f"PnL: {self.stats['pnl_usdc']:+.2f} USDC"
        )

        log.info(f"Polling every {POLL_SECONDS}s...")
        while True:
            try:
                self._tick()
            except Exception as e:
                log.error(f"Tick error: {e}", exc_info=True)
            time.sleep(POLL_SECONDS)

    def _tick(self):
        price = fetch_price_usd()
        if price is None:
            return

        self.builder.push(price)
        closed = self.builder.closed_candles()
        live   = self.builder.live_candle()

        # Exit check on live candle
        if self.position and live:
            result = self.position.check_exit(live)
            if result:
                ep = self.position.sl if result == "SL" else self.position.tp
                self._close_trade(result, ep)

        if len(closed) < MIN_CANDLES:
            log.info(
                f"MON/USD={price:.6f} | Warming up "
                f"{len(closed)}/{MIN_CANDLES} candles "
                f"(~{(MIN_CANDLES - len(closed)) * 3} min left)"
            )
            return

        closes   = [c["close"] for c in closed]
        rsi_vals = calc_rsi(closes, RSI_PERIOD)
        wma_vals = calc_wma(closes, WMA_PERIOD)

        rsi_now = next((v for v in reversed(rsi_vals) if v is not None), None)
        wma_now = next((v for v in reversed(wma_vals) if v is not None), None)

        if not self.position:
            last_c = closed[-1]
            if last_c["ts"] != self._last_signal_ts:
                cross = detect_crossover(rsi_vals, wma_vals)
                if cross == "bull":
                    self._last_signal_ts = last_c["ts"]
                    self._open_trade(last_c, price)

        pos_str = (f"IN TRADE entry={self.position.entry:.6f}" if self.position else "FLAT")
        log.info(
            f"MON={price:.6f}  RSI={rsi_now:.2f}  WMA={wma_now:.6f}  "
            f"Candles={len(closed)}  {pos_str}"
        )

    def _open_trade(self, candle, curr_price):
        entry = curr_price
        sl    = candle["low"]
        risk  = entry - sl
        if risk <= 0:
            log.warning("SL >= entry — skipping.")
            return

        size_usdc = min((ACCOUNT_BALANCE * 0.01) / (risk / entry), ACCOUNT_BALANCE * 0.95)
        tp        = entry + risk * TP_MULTIPLIER
        self.position = Position(entry, sl, tp, size_usdc)

        sl_pct = (entry - sl) / entry * 100
        tp_pct = (tp - entry) / entry * 100
        comm   = size_usdc * COMMISSION_RT
        ts_str = self.position.opened_at.strftime("%Y-%m-%d %H:%M UTC")

        log.info(f">>> LONG OPENED  entry={entry:.6f}  SL={sl:.6f}({sl_pct:.2f}%)  TP={tp:.6f}({tp_pct:.2f}%)  size={size_usdc:.2f}")
        tg_send(
            "*LONG OPENED* - MON/USD\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"Entry : `{entry:.6f}`\n"
            f"SL    : `{sl:.6f}` ({sl_pct:.2f}%)\n"
            f"TP    : `{tp:.6f}` ({tp_pct:.2f}%)\n"
            f"Size  : `{size_usdc:.2f} USDC` (paper)\n"
            f"Comm  : `{comm:.4f} USDC`\n"
            f"Time  : `{ts_str}`\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"W:{self.stats['wins']} L:{self.stats['losses']} | PnL: {self.stats['pnl_usdc']:+.2f} USDC"
        )

    def _close_trade(self, result, exit_price):
        pos  = self.position
        pnl  = pos.pnl(exit_price)
        mins = int((datetime.now(timezone.utc) - pos.opened_at).total_seconds() / 60)

        with self._lock:
            if result == "TP":
                self.stats["wins"] += 1
            else:
                self.stats["losses"] += 1
            self.stats["pnl_usdc"] = round(self.stats["pnl_usdc"] + pnl, 4)
            total = self.stats["wins"] + self.stats["losses"]
            acc   = self.stats["wins"] / total * 100 if total else 0.0
            self.stats["trades"].append({
                "entry": pos.entry, "exit": exit_price,
                "sl": pos.sl, "tp": pos.tp,
                "size_usdc": pos.size_usdc, "result": result,
                "pnl": round(pnl, 4), "duration_min": mins,
                "time": datetime.now(timezone.utc).isoformat(),
            })
            save_stats(self.stats)

        emoji = "\u2705" if result == "TP" else "\u274c"
        log.info(
            f">>> TRADE CLOSED {result}  exit={exit_price:.6f}  pnl={pnl:+.4f}  "
            f"W:{self.stats['wins']} L:{self.stats['losses']}  acc={acc:.1f}%  "
            f"total={self.stats['pnl_usdc']:+.2f}"
        )
        tg_send(
            f"{emoji} *TRADE CLOSED - {result}* MON/USD\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"Entry    : `{pos.entry:.6f}`\n"
            f"Exit     : `{exit_price:.6f}`\n"
            f"Duration : `{mins} min`\n"
            f"PnL      : `{pnl:+.4f} USDC`\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"W:{self.stats['wins']} L:{self.stats['losses']} | Acc: {acc:.1f}%\n"
            f"Total PnL: `{self.stats['pnl_usdc']:+.2f} USDC`"
        )
        self.position = None

# ─── ENTRY POINT ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot = TradingBot()
    bot.run()
