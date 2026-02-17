"""
╔══════════════════════════════════════════════════════════════╗
║         VAULT777 MARKETS — 5-MIN VOLATILITY TRADING BOT     ║
║   Monitors BTC, ETH, SOL • Auto-bets Over/Under on Vault777 ║
╚══════════════════════════════════════════════════════════════╝

SETUP:
  pip install requests python-dotenv colorama tabulate

  Create .env file:
    VAULT777_SESSION=<your session cookie from DevTools>
    BET_AMOUNT_USD=1.00
    DRY_RUN=true               # set false for real bets
    CONFIDENCE_THRESHOLD=0.60

HOW TO GET YOUR SESSION COOKIE:
  1. Log in to markets.vault777.com
  2. F12 → Application → Cookies → copy "session" value
  3. Paste into .env as VAULT777_SESSION=...

RUN: python vault777_bot.py
"""

import os, sys, time, logging, json, datetime, statistics
from collections import deque
from dotenv import load_dotenv
import requests
from colorama import Fore, Style, init as colorama_init
from tabulate import tabulate

colorama_init(autoreset=True)
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("vault777_bot.log"), logging.StreamHandler()],
)
log = logging.getLogger("vault777_bot")

# ── Config ──────────────────────────────────────────────────────────────────
VAULT777_BASE    = "https://markets.vault777.com"
COINGECKO_API    = "https://api.coingecko.com/api/v3"
SESSION_COOKIE   = os.getenv("VAULT777_SESSION", "")
BET_AMOUNT_USD   = float(os.getenv("BET_AMOUNT_USD", "1.00"))
DRY_RUN          = os.getenv("DRY_RUN", "true").lower() != "false"
CONFIDENCE_MIN   = float(os.getenv("CONFIDENCE_THRESHOLD", "0.60"))
POLL_INTERVAL    = 30   # seconds
COINS            = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}

price_history    = {sym: deque(maxlen=30) for sym in COINS}
last_trade_window = {}
trade_log        = []

# ── Price Feed (CoinGecko — free, no API key) ───────────────────────────────
def fetch_prices():
    ids = ",".join(COINS.values())
    try:
        r = requests.get(f"{COINGECKO_API}/simple/price",
                         params={"ids": ids, "vs_currencies": "usd"}, timeout=10)
        r.raise_for_status()
        data = r.json()
        return {sym: float(data[cg]["usd"]) for sym, cg in COINS.items() if cg in data}
    except Exception as e:
        log.warning(f"Price fetch failed: {e}")
        return {}

# ── Signal Engine ───────────────────────────────────────────────────────────
def compute_signal(sym):
    hist = list(price_history[sym])
    if len(hist) < 4:
        return {"signal": "HOLD", "confidence": 0.0, "price": 0,
                "volatility": 0, "momentum_1m": 0, "momentum_5m": 0}

    prices = [p for _, p in hist]
    pct    = [(prices[i]-prices[i-1])/prices[i-1]*100 for i in range(1, len(prices))]
    vol    = statistics.stdev(pct[-10:]) if len(pct) >= 2 else 0.0

    p1m   = prices[max(-3,  -len(prices))]
    p5m   = prices[max(-11, -len(prices))]
    mom1m = (prices[-1] - p1m) / p1m * 100
    mom5m = (prices[-1] - p5m) / p5m * 100

    HIGH_VOL = 0.8  # % std-dev — above this is too noisy to bet safely

    if vol > HIGH_VOL:
        sig, conf = "HOLD", 0.0
    elif mom5m > 0.05 and mom1m > 0:
        sig  = "OVER"
        conf = min(0.95, 0.50 + abs(mom5m) / (vol + 0.01) * 0.10)
    elif mom5m < -0.05 and mom1m < 0:
        sig  = "UNDER"
        conf = min(0.95, 0.50 + abs(mom5m) / (vol + 0.01) * 0.10)
    else:
        sig, conf = "HOLD", 0.30

    return {"signal": sig, "confidence": round(conf, 3), "price": prices[-1],
            "volatility": round(vol, 4), "momentum_1m": round(mom1m, 4),
            "momentum_5m": round(mom5m, 4)}

# ── Market Slug ─────────────────────────────────────────────────────────────
def current_slug(sym, interval="5min"):
    now    = datetime.datetime.utcnow()
    minute = (now.minute // 5) * 5
    t      = now.replace(minute=minute, second=0, microsecond=0)
    return f"{sym.lower()}-{interval}-{t.strftime('%Y-%m-%d-%H%M')}"

# ── Betting ─────────────────────────────────────────────────────────────────
def place_bet(slug, side, amount):
    if DRY_RUN:
        log.info(f"{Fore.YELLOW}[DRY RUN]{Style.RESET_ALL} ${amount:.2f} {side} → {slug}")
        return True
    if not SESSION_COOKIE:
        log.error("Set VAULT777_SESSION in .env")
        return False
    headers = {
        "Cookie":       f"session={SESSION_COOKIE}",
        "Content-Type": "application/json",
        "Origin":       VAULT777_BASE,
        "Referer":      f"{VAULT777_BASE}/markets/{slug}",
    }
    payload = {"marketSlug": slug, "outcome": side.lower(), "amount": amount}
    try:
        r = requests.post(f"{VAULT777_BASE}/api/bets", json=payload,
                          headers=headers, timeout=15)
        if r.status_code in (200, 201):
            log.info(f"{Fore.GREEN}✅ BET PLACED{Style.RESET_ALL}: ${amount:.2f} {side} → {slug}")
            return True
        log.error(f"Bet failed [{r.status_code}]: {r.text[:200]}")
        return False
    except Exception as e:
        log.error(f"Bet request error: {e}")
        return False

# ── Decision Engine ─────────────────────────────────────────────────────────
def evaluate_and_trade(sym):
    sig  = compute_signal(sym)
    slug = current_slug(sym)

    if last_trade_window.get(sym) == slug:
        return sig  # already acted on this window

    if sig["signal"] == "HOLD" or sig["confidence"] < CONFIDENCE_MIN:
        log.info(f"{sym:3s}  HOLD  conf={sig['confidence']:.0%}  "
                 f"vol={sig['volatility']:.4f}%  5m={sig['momentum_5m']:+.4f}%")
        return sig

    if place_bet(slug, sig["signal"], BET_AMOUNT_USD):
        last_trade_window[sym] = slug
        record = {
            "ts": datetime.datetime.utcnow().isoformat(),
            "symbol": sym, "slug": slug, "side": sig["signal"],
            "amount": BET_AMOUNT_USD, "confidence": sig["confidence"],
            "price": sig["price"], "volatility": sig["volatility"],
            "mom_5m": sig["momentum_5m"], "dry_run": DRY_RUN,
        }
        trade_log.append(record)
        with open("vault777_trades.json", "w") as f:
            json.dump(trade_log, f, indent=2)
    return sig

# ── Terminal Dashboard ──────────────────────────────────────────────────────
def print_dashboard(signals):
    rows = []
    for sym, sig in signals.items():
        s = sig.get("signal", "HOLD")
        c = Fore.GREEN if s == "OVER" else Fore.RED if s == "UNDER" else Fore.YELLOW
        rows.append([
            f"{c}{sym}{Style.RESET_ALL}",
            f"${sig.get('price', 0):,.2f}",
            f"{sig.get('volatility', 0):.4f}%",
            f"{sig.get('momentum_1m', 0):+.4f}%",
            f"{sig.get('momentum_5m', 0):+.4f}%",
            f"{c}{s}{Style.RESET_ALL}",
            f"{sig.get('confidence', 0):.0%}",
        ])
    now = datetime.datetime.utcnow().strftime("%H:%M:%S UTC")
    win = current_slug("BTC").replace("btc-5min-", "")
    print("\033[H\033[J", end="")
    print(f"{Fore.CYAN}{'═'*68}")
    print(f"  VAULT777 BOT  │  5-Min Over/Under  │  {now}  │  DRY={DRY_RUN}")
    print(f"  Window: {win}  │  Trades: {len(trade_log)}")
    print(f"{'═'*68}{Style.RESET_ALL}")
    print(tabulate(rows,
                   headers=["Coin","Price","Vol","Mom 1m","Mom 5m","Signal","Conf"],
                   tablefmt="rounded_outline"))
    if trade_log:
        print(f"\n  Recent Trades:")
        for t in trade_log[-4:]:
            c  = Fore.GREEN if t["side"] == "OVER" else Fore.RED
            dr = " [DRY]" if t["dry_run"] else ""
            print(f"    {t['ts'][11:19]}  {t['symbol']:3}  "
                  f"{c}{t['side']:<5}{Style.RESET_ALL}  "
                  f"{t['confidence']:.0%}  ${t['amount']:.2f}{dr}")
    print(f"\n  MinConf={CONFIDENCE_MIN:.0%}  BetSize=${BET_AMOUNT_USD:.2f}")

# ── Main Loop ───────────────────────────────────────────────────────────────
def main():
    log.info(f"VAULT777 BOT | DryRun={DRY_RUN} | Bet=${BET_AMOUNT_USD} | MinConf={CONFIDENCE_MIN:.0%}")
    if not SESSION_COOKIE and not DRY_RUN:
        log.error("VAULT777_SESSION not set. Aborting.")
        sys.exit(1)

    while True:
        try:
            prices = fetch_prices()
            if prices:
                ts = datetime.datetime.utcnow()
                for sym, price in prices.items():
                    price_history[sym].append((ts, price))
                signals = {sym: evaluate_and_trade(sym) for sym in COINS if sym in prices}
                print_dashboard(signals)
        except Exception as e:
            log.warning(f"Poll error: {e}")
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}Bot stopped.{Style.RESET_ALL}")