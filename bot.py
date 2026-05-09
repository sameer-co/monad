#!/usr/bin/env python3
"""
MON/USDC & MON/USDT RSI(40) x WMA(15) Crossover Trading Bot
3-minute timeframe | 100 USDC account | 0.08% round-trip commission
TP = 2.2x SL | Telegram alerts | Win/Loss tracker
"""

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

import os
import time
import json
import logging
import requests
import threading
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ─── CONFIG ──────────────────────────────────────────────────────────────────
ACCOUNT_BALANCE   = 100.0          # USDC
COMMISSION_RT     = 0.0008         # 0.08% round-trip
RSI_PERIOD        = 40
WMA_PERIOD        = 15
TP_MULTIPLIER     = 2.2
TIMEFRAME         = "3m"
INTERVAL_SECONDS  = 180            # 3 min poll
SYMBOLS           = ["MONUSDC", "MONUSDT"]
STATS_FILE        = Path("stats.json")

TG_TOKEN  = os.getenv("TG_BOT_TOKEN",  "8349229275:AAGNWV2A0_Pf9LhlwZCczeBoMcUaJL2shFg")
TG_CHAT   = os.getenv("TG_CHAT_ID", "1950462171")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("MON-BOT")

# ─── STATS PERSISTENCE ───────────────────────────────────────────────────────
def load_stats() -> dict:
    if STATS_FILE.exists():
        try:
            return json.loads(STATS_FILE.read_text())
        except Exception:
            pass
    return {"wins": 0, "losses": 0, "pnl_usdc": 0.0, "trades": []}

def save_stats(s: dict):
    STATS_FILE.write_text(json.dumps(s, indent=2))

# ─── TELEGRAM ────────────────────────────────────────────────────────────────
def tg_send(msg: str):
    if not TG_TOKEN or not TG_CHAT:
        log.warning("Telegram not configured – skipping alert.")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"}, timeout=10)
        if not r.ok:
            log.warning(f"TG send failed: {r.text}")
    except Exception as e:
        log.warning(f"TG error: {e}")

# ─── EXCHANGE DATA (Binance public) ──────────────────────────────────────────
BASE = "https://api.binance.com"

def fetch_klines(symbol: str, interval: str = "3m", limit: int = 100) -> list[dict]:
    """Fetch OHLCV candles from Binance public endpoint."""
    url = f"{BASE}/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    candles = []
    for k in r.json():
        candles.append({
            "ts":    k[0],
            "open":  float(k[1]),
            "high":  float(k[2]),
            "low":   float(k[3]),
            "close": float(k[4]),
            "vol":   float(k[5]),
            "closed": k[6] < int(time.time() * 1000),  # True if candle closed
        })
    return candles

def resolve_symbol(symbols: list[str]) -> str | None:
    """Return first symbol that exists on Binance."""
    for sym in symbols:
        try:
            r = requests.get(f"{BASE}/api/v3/ticker/price", params={"symbol": sym}, timeout=5)
            if r.ok and "price" in r.json():
                log.info(f"Active symbol: {sym}")
                return sym
        except Exception:
            pass
    return None

# ─── INDICATORS ──────────────────────────────────────────────────────────────
def calc_wma(closes: list[float], period: int) -> list[float]:
    """Weighted Moving Average."""
    wma = []
    weights = list(range(1, period + 1))
    denom = sum(weights)
    for i in range(len(closes)):
        if i < period - 1:
            wma.append(None)
        else:
            window = closes[i - period + 1 : i + 1]
            val = sum(w * v for w, v in zip(weights, window)) / denom
            wma.append(val)
    return wma

def calc_rsi(closes: list[float], period: int) -> list[float]:
    """RSI using Wilder's smoothing (EMA-based)."""
    rsi = [None] * period
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))

    if len(gains) < period:
        return [None] * len(closes)

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            rsi.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi.append(100 - 100 / (1 + rs))

    return rsi

def detect_crossover(rsi: list, wma: list) -> str | None:
    """
    Returns 'bull' if RSI crossed above WMA on the last closed candle,
    'bear' if RSI crossed below WMA, else None.
    """
    valid = [(r, w) for r, w in zip(rsi, wma) if r is not None and w is not None]
    if len(valid) < 2:
        return None
    prev_r, prev_w = valid[-2]
    curr_r, curr_w = valid[-1]
    if prev_r <= prev_w and curr_r > curr_w:
        return "bull"
    if prev_r >= prev_w and curr_r < curr_w:
        return "bear"
    return None

# ─── POSITION MANAGEMENT ─────────────────────────────────────────────────────
class Position:
    def __init__(self, symbol, side, entry, sl, tp, size_usdc, candle_low, candle_high):
        self.symbol     = symbol
        self.side       = side        # "LONG" only for now
        self.entry      = entry
        self.sl         = sl
        self.tp         = tp
        self.size_usdc  = size_usdc
        self.candle_low = candle_low
        self.candle_high= candle_high
        self.opened_at  = datetime.now(timezone.utc)
        self.qty        = size_usdc / entry   # approximate units

    def check_exit(self, candle: dict):
        """Returns 'TP', 'SL', or None based on candle high/low."""
        if self.side == "LONG":
            if candle["low"] <= self.sl:
                return "SL"
            if candle["high"] >= self.tp:
                return "TP"
        return None

    def pnl(self, exit_price: float) -> float:
        gross = (exit_price - self.entry) / self.entry * self.size_usdc
        commission = self.size_usdc * COMMISSION_RT
        return gross - commission

# ─── BOT STATE ───────────────────────────────────────────────────────────────
class TradingBot:
    def __init__(self):
        self.symbol   = None
        self.position: Position | None = None
        self.stats    = load_stats()
        self._lock    = threading.Lock()
        self._last_signal_candle_ts = 0

    def resolve(self):
        self.symbol = resolve_symbol(SYMBOLS)
        if not self.symbol:
            log.error("Neither MONUSDC nor MONUSDT found on Binance. Exiting.")
            sys.exit(1)

    def run(self):
        self.resolve()
        log.info(f"Bot started → symbol={self.symbol} tf={TIMEFRAME} RSI={RSI_PERIOD} WMA={WMA_PERIOD}")
        tg_send(f"🤖 *MON Bot Started*\nSymbol: `{self.symbol}`\nTF: `{TIMEFRAME}` | RSI({RSI_PERIOD}) × WMA({WMA_PERIOD})\nAccount: `{ACCOUNT_BALANCE} USDC`")

        while True:
            try:
                self._tick()
            except Exception as e:
                log.error(f"Tick error: {e}", exc_info=True)
            time.sleep(INTERVAL_SECONDS)

    def _tick(self):
        candles = fetch_klines(self.symbol, TIMEFRAME, limit=max(RSI_PERIOD * 2, 120))
        if len(candles) < RSI_PERIOD + WMA_PERIOD + 5:
            log.warning("Not enough candles yet.")
            return

        # Use only closed candles for signal, last candle for position monitoring
        closed = [c for c in candles if c["closed"]]
        last_live = candles[-1]  # current (possibly open) candle for exit checks

        closes = [c["close"] for c in closed]
        rsi_vals = calc_rsi(closes, RSI_PERIOD)
        wma_vals = calc_wma(closes, WMA_PERIOD)

        curr_price = last_live["close"]

        # ── Position exit check ──────────────────────────────────────────────
        if self.position:
            result = self.position.check_exit(last_live)
            if result:
                exit_price = self.position.sl if result == "SL" else self.position.tp
                self._close_trade(result, exit_price)
                return

        # ── Entry signal (only on last CLOSED candle, no duplicate) ─────────
        if not self.position and closed:
            last_closed = closed[-1]
            if last_closed["ts"] != self._last_signal_candle_ts:
                crossover = detect_crossover(rsi_vals, wma_vals)
                if crossover == "bull":
                    self._last_signal_candle_ts = last_closed["ts"]
                    self._open_trade(last_closed, curr_price)

        # ── Status log ──────────────────────────────────────────────────────
        rsi_now = next((v for v in reversed(rsi_vals) if v is not None), None)
        wma_now = next((v for v in reversed(wma_vals) if v is not None), None)
        pos_str = f"IN TRADE entry={self.position.entry:.6f}" if self.position else "FLAT"
        log.info(f"[{self.symbol}] Price={curr_price:.6f} RSI={rsi_now:.2f} WMA={wma_now:.6f} | {pos_str}")

    def _open_trade(self, candle: dict, curr_price: float):
        entry = curr_price
        sl    = candle["low"]          # SL below crossover candle low
        risk  = entry - sl
        tp    = entry + risk * TP_MULTIPLIER

        if risk <= 0:
            log.warning("Invalid SL (risk ≤ 0) – skipping trade.")
            return

        # Position sizing: risk 1% of account (adjustable) minus commission
        risk_usdc = ACCOUNT_BALANCE * 0.01
        size_usdc = min(risk_usdc / (risk / entry), ACCOUNT_BALANCE * 0.95)

        self.position = Position(self.symbol, "LONG", entry, sl, tp, size_usdc, candle["low"], candle["high"])

        sl_pct = (entry - sl) / entry * 100
        tp_pct = (tp - entry) / entry * 100
        commission_usdc = size_usdc * COMMISSION_RT

        msg = (
            f"🟢 *LONG OPENED* — `{self.symbol}`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📈 Entry : `{entry:.6f}`\n"
            f"🛑 SL    : `{sl:.6f}` ({sl_pct:.2f}%)\n"
            f"🎯 TP    : `{tp:.6f}` ({tp_pct:.2f}%)\n"
            f"💰 Size  : `{size_usdc:.2f} USDC`\n"
            f"💸 Commission: `{commission_usdc:.4f} USDC`\n"
            f"⏰ {self.position.opened_at.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📊 W:{self.stats['wins']} L:{self.stats['losses']} | PnL: {self.stats['pnl_usdc']:+.2f} USDC"
        )
        log.info(f"LONG OPENED | entry={entry:.6f} SL={sl:.6f} TP={tp:.6f} size={size_usdc:.2f}")
        tg_send(msg)

    def _close_trade(self, result: str, exit_price: float):
        pos = self.position
        pnl = pos.pnl(exit_price)
        duration = datetime.now(timezone.utc) - pos.opened_at
        mins = int(duration.total_seconds() / 60)

        with self._lock:
            if result == "TP":
                self.stats["wins"] += 1
            else:
                self.stats["losses"] += 1
            self.stats["pnl_usdc"] = round(self.stats["pnl_usdc"] + pnl, 4)
            total = self.stats["wins"] + self.stats["losses"]
            acc = self.stats["wins"] / total * 100 if total else 0
            self.stats["trades"].append({
                "symbol":   pos.symbol,
                "side":     pos.side,
                "entry":    pos.entry,
                "exit":     exit_price,
                "sl":       pos.sl,
                "tp":       pos.tp,
                "result":   result,
                "pnl":      round(pnl, 4),
                "duration_min": mins,
                "time":     datetime.now(timezone.utc).isoformat(),
            })
            save_stats(self.stats)

        emoji = "✅" if result == "TP" else "❌"
        msg = (
            f"{emoji} *TRADE CLOSED — {result}* `{pos.symbol}`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📈 Entry  : `{pos.entry:.6f}`\n"
            f"📉 Exit   : `{exit_price:.6f}`\n"
            f"{'🎯' if result=='TP' else '🛑'} {'TP' if result=='TP' else 'SL'}    : `{exit_price:.6f}`\n"
            f"⏱ Duration: `{mins} min`\n"
            f"💵 PnL    : `{pnl:+.4f} USDC`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📊 *Stats* | W:{self.stats['wins']} L:{self.stats['losses']} | Acc: {acc:.1f}%\n"
            f"💰 Total PnL: `{self.stats['pnl_usdc']:+.2f} USDC`"
        )
        log.info(f"TRADE CLOSED {result} | exit={exit_price:.6f} pnl={pnl:+.4f}")
        tg_send(msg)
        self.position = None

# ─── ENTRY POINT ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot = TradingBot()
    bot.run()
