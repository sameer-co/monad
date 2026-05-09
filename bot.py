#!/usr/bin/env python3
"""
MON/USDT Paper Trading Bot — RSI(40) × WMA(15) Crossover
Exchange : OKX public REST API (no key needed)
TF        : 3-minute real OHLCV candles
Account   : 100 USDC paper | 0.08% round-trip commission
SL        : Below crossover candle low | TP : risk × 2.2
Alerts    : Telegram on open & close | Stats: stats.json
"""

# ── Auto-install ───────────────────────────────────────────────────────────────
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

import os, time, json, logging, threading
from datetime import datetime, timezone
from pathlib import Path
import requests
from dotenv import load_dotenv

load_dotenv()

# ── CONFIG ────────────────────────────────────────────────────────────────────
ACCOUNT_BALANCE = 100.0
COMMISSION_RT   = 0.0008
RSI_PERIOD      = 40
WMA_PERIOD      = 15
TP_MULTIPLIER   = 2.2
POLL_SECONDS    = 60          # poll every 60s (3m candle updates are smooth)
MIN_CANDLES     = RSI_PERIOD + WMA_PERIOD + 5

BASE_DIR   = Path(os.getenv("BOT_DIR", Path(__file__).parent))
STATS_FILE = BASE_DIR / "stats.json"
LOG_FILE   = BASE_DIR / "bot.log"

OKX_BASE   = "https://www.okx.com"
OKX_SYMBOL = "MON-USDT"

TG_TOKEN = os.getenv("TG_BOT_TOKEN", "8349229275:AAGNWV2A0_Pf9LhlwZCczeBoMcUaJL2shFg")
TG_CHAT  = os.getenv("TG_CHAT_ID",   "1950462171")

# ── LOGGING ───────────────────────────────────────────────────────────────────
BASE_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("MON-BOT")

# ── STATS ─────────────────────────────────────────────────────────────────────
def load_stats():
    if STATS_FILE.exists():
        try:
            return json.loads(STATS_FILE.read_text())
        except Exception:
            pass
    return {"wins": 0, "losses": 0, "pnl_usdc": 0.0, "trades": []}

def save_stats(s):
    STATS_FILE.write_text(json.dumps(s, indent=2))

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def tg_send(msg):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
        if not r.ok:
            log.warning(f"TG failed: {r.status_code} {r.text[:100]}")
    except Exception as e:
        log.warning(f"TG error: {e}")

# ── OKX DATA ──────────────────────────────────────────────────────────────────
_sess = requests.Session()
_sess.headers.update({"User-Agent": "MON-PaperBot/2.0", "Accept": "application/json"})

def fetch_candles(limit=200):
    """
    OKX /api/v5/market/candles
    Returns: [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]
    confirm='1' = candle closed. Returns oldest→newest.
    """
    try:
        r = _sess.get(
            f"{OKX_BASE}/api/v5/market/candles",
            params={"instId": OKX_SYMBOL, "bar": "3m", "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        raw = r.json()
        if raw.get("code") != "0":
            log.warning(f"OKX API error: {raw.get('msg')}")
            return []
        candles = []
        for row in reversed(raw["data"]):   # newest-first → reverse to oldest-first
            candles.append({
                "ts":     int(row[0]),
                "open":   float(row[1]),
                "high":   float(row[2]),
                "low":    float(row[3]),
                "close":  float(row[4]),
                "closed": row[8] == "1",
            })
        return candles
    except Exception as e:
        log.warning(f"OKX fetch error: {e}")
        return []

def fetch_ticker():
    try:
        r = _sess.get(
            f"{OKX_BASE}/api/v5/market/ticker",
            params={"instId": OKX_SYMBOL},
            timeout=8,
        )
        r.raise_for_status()
        d = r.json()
        if d.get("code") == "0":
            return float(d["data"][0]["last"])
    except Exception:
        pass
    return None

def test_connectivity():
    """Returns True if OKX is reachable and MON-USDT exists."""
    price = fetch_ticker()
    if price:
        log.info(f"OKX connected — MON/USDT = {price:.6f}")
        return True
    log.error("Cannot reach OKX API. Check your server location / firewall.")
    return False

# ── INDICATORS ────────────────────────────────────────────────────────────────
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

def detect_crossover(rsi_vals, wma_vals):
    pairs = [(r, w) for r, w in zip(rsi_vals, wma_vals)
             if r is not None and w is not None]
    if len(pairs) < 2:
        return None
    pr, pw = pairs[-2]
    cr, cw = pairs[-1]
    if pr <= pw and cr > cw:
        return "bull"
    if pr >= pw and cr < cw:
        return "bear"
    return None

# ── POSITION ──────────────────────────────────────────────────────────────────
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
        return (exit_price - self.entry) / self.entry * self.size_usdc \
               - self.size_usdc * COMMISSION_RT

# ── BOT ───────────────────────────────────────────────────────────────────────
class TradingBot:
    def __init__(self):
        self.stats           = load_stats()
        self.position        = None
        self._lock           = threading.Lock()
        self._last_signal_ts = 0

    def run(self):
        log.info("=" * 60)
        log.info("  MON/USDT PAPER BOT  |  RSI(40) x WMA(15)  |  3m TF")
        log.info(f"  Paper: {ACCOUNT_BALANCE} USDC  |  Exchange: OKX (public API)")
        log.info("=" * 60)

        # ── connectivity check with retries ──────────────────────────────────
        for attempt in range(1, 7):
            if test_connectivity():
                break
            log.warning(f"Retry {attempt}/6 in 10s...")
            time.sleep(10)
        else:
            tg_send("*MON Bot ERROR*: Cannot reach OKX. Check server location.")
            sys.exit(1)

        tg_send(
            "*MON Paper Bot Started* \U0001f916\n"
            "------------------------------------\n"
            f"Strategy  : RSI({RSI_PERIOD}) x WMA({WMA_PERIOD})\n"
            f"Timeframe : 3-minute\n"
            f"Symbol    : `{OKX_SYMBOL}` on OKX\n"
            f"Account   : `{ACCOUNT_BALANCE} USDC` (paper)\n"
            "------------------------------------\n"
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
        candles = fetch_candles(limit=200)
        if not candles:
            log.warning("Empty candle response — skipping tick.")
            return

        # Split closed vs live
        closed = [c for c in candles if c["closed"]]
        live   = next((c for c in reversed(candles) if not c["closed"]), None)
        # Fallback: if exchange marks all as closed, treat last as live
        if not live and candles:
            live = candles[-1]
            closed = candles[:-1]

        curr_price = live["close"] if live else (closed[-1]["close"] if closed else None)
        if curr_price is None:
            return

        # ── Exit check on live candle ────────────────────────────────────────
        if self.position and live:
            result = self.position.check_exit(live)
            if result:
                ep = self.position.sl if result == "SL" else self.position.tp
                self._close_trade(result, ep)

        # ── Need enough closed candles ───────────────────────────────────────
        if len(closed) < MIN_CANDLES:
            log.info(f"MON={curr_price:.6f} | Warming up {len(closed)}/{MIN_CANDLES} candles")
            return

        # ── Indicators ───────────────────────────────────────────────────────
        closes   = [c["close"] for c in closed]
        rsi_vals = calc_rsi(closes, RSI_PERIOD)
        wma_vals = calc_wma(closes, WMA_PERIOD)
        rsi_now  = next((v for v in reversed(rsi_vals) if v is not None), None)
        wma_now  = next((v for v in reversed(wma_vals) if v is not None), None)

        # ── Entry signal ─────────────────────────────────────────────────────
        if not self.position and closed:
            last_c = closed[-1]
            if last_c["ts"] != self._last_signal_ts:
                cross = detect_crossover(rsi_vals, wma_vals)
                if cross == "bull":
                    self._last_signal_ts = last_c["ts"]
                    self._open_trade(last_c, curr_price)

        # ── Status log ───────────────────────────────────────────────────────
        pos_str = (
            f"IN TRADE  entry={self.position.entry:.6f}  "
            f"SL={self.position.sl:.6f}  TP={self.position.tp:.6f}"
            if self.position else "FLAT"
        )
        log.info(
            f"MON={curr_price:.6f}  RSI={rsi_now:.2f}  WMA={wma_now:.6f}  "
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

        log.info(
            f">>> LONG OPENED  entry={entry:.6f}  "
            f"SL={sl:.6f}({sl_pct:.2f}%)  TP={tp:.6f}({tp_pct:.2f}%)  "
            f"size={size_usdc:.2f} USDC"
        )
        tg_send(
            "*LONG OPENED* - MON/USDT\n"
            "------------------------------------\n"
            f"Entry : `{entry:.6f}`\n"
            f"SL    : `{sl:.6f}` ({sl_pct:.2f}%)\n"
            f"TP    : `{tp:.6f}` ({tp_pct:.2f}%)\n"
            f"Size  : `{size_usdc:.2f} USDC` (paper)\n"
            f"Comm  : `{comm:.4f} USDC`\n"
            f"Time  : `{ts_str}`\n"
            "------------------------------------\n"
            f"W:{self.stats['wins']} L:{self.stats['losses']} | "
            f"PnL: {self.stats['pnl_usdc']:+.2f} USDC"
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
            f"W:{self.stats['wins']} L:{self.stats['losses']}  "
            f"acc={acc:.1f}%  total={self.stats['pnl_usdc']:+.2f}"
        )
        tg_send(
            f"{emoji} *TRADE CLOSED - {result}* MON/USDT\n"
            "------------------------------------\n"
            f"Entry    : `{pos.entry:.6f}`\n"
            f"Exit     : `{exit_price:.6f}`\n"
            f"Duration : `{mins} min`\n"
            f"PnL      : `{pnl:+.4f} USDC`\n"
            "------------------------------------\n"
            f"W:{self.stats['wins']} L:{self.stats['losses']} | Acc: {acc:.1f}%\n"
            f"Total PnL: `{self.stats['pnl_usdc']:+.2f} USDC`"
        )
        self.position = None

# ── ENTRY POINT ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot = TradingBot()
    bot.run()
