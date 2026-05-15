#!/usr/bin/env python3
"""
kairos-data.py — Live Data Fetcher for Kairos Technical Analysis

Fetches kline/candlestick data, technical indicators, and market metrics
from public APIs. Designed to be run from the Kairos trading skill.

Usage:
    python3 kairos-data.py --coin bitcoin
    python3 kairos-data.py --coin ethereum --timeframe 4h
    python3 kairos-data.py --coin solana --json
    python3 kairos-data.py --list-coins

Dependencies: python3 standard lib (urllib, json)
If 'numpy' is available, uses it for indicator calculations.
"""

import argparse
import functools
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ── Retry Decorator ────────────────────────────────────────────────────────────

def retry(max_attempts=3, delay=2):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_attempts - 1:
                        raise
                    print(f'  [retry] Attempt {attempt+1} failed: {e}. Retrying in {delay}s...', file=sys.stderr)
                    time.sleep(delay)
            return None
        return wrapper
    return decorator

# ── Cache Decorator ────────────────────────────────────────────────────────────

CACHE_DIR = os.path.expanduser('~/.cache/telos-agents')
os.makedirs(CACHE_DIR, exist_ok=True)

def cached(ttl_seconds=300):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            cache_key = f'{func.__name__}_{hash(str(args) + str(sorted(kwargs.items())))}'
            cache_path = os.path.join(CACHE_DIR, f'{cache_key}.json')
            if os.path.exists(cache_path):
                age = time.time() - os.path.getmtime(cache_path)
                if age < ttl_seconds:
                    with open(cache_path) as f:
                        return json.load(f)
            result = func(*args, **kwargs)
            with open(cache_path, 'w') as f:
                json.dump(result, f)
            return result
        return wrapper
    return decorator

# ── Configuration ──────────────────────────────────────────────────────────────

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
BINANCE_BASE = "https://api.binance.com/api/v3"

# Known coin ID mappings (CoinGecko IDs)
COIN_IDS = {
    "bitcoin": "bitcoin", "btc": "bitcoin",
    "ethereum": "ethereum", "eth": "ethereum",
    "solana": "solana", "sol": "solana",
    "cardano": "cardano", "ada": "cardano",
    "ripple": "ripple", "xrp": "ripple",
    "polkadot": "polkadot", "dot": "polkadot",
    "avalanche": "avalanche-2", "avax": "avalanche-2",
    "chainlink": "chainlink", "link": "chainlink",
    "polygon": "matic-network", "matic": "matic-network",
    "arbitrum": "arbitrum", "arb": "arbitrum",
    "optimism": "optimism", "op": "optimism",
    "sui": "sui", "aptos": "aptos", "apt": "aptos",
    "near": "near", "injective": "injective-protocol", "inj": "injective-protocol",
    "render": "render-token", "rndr": "render-token",
    "ai16z": "ai16z", "virtuals": "virtual-protocol", "virtual": "virtual-protocol",
}

# Binance symbol mapping (common coins)
BINANCE_SYMBOLS = {
    "bitcoin": "BTCUSDT", "btc": "BTCUSDT",
    "ethereum": "ETHUSDT", "eth": "ETHUSDT",
    "solana": "SOLUSDT", "sol": "SOLUSDT",
    "cardano": "ADAUSDT", "ada": "ADAUSDT",
    "ripple": "XRPUSDT", "xrp": "XRPUSDT",
    "polkadot": "DOTUSDT", "dot": "DOTUSDT",
    "avalanche": "AVAXUSDT", "avax": "AVAXUSDT",
    "chainlink": "LINKUSDT", "link": "LINKUSDT",
    "polygon": "MATICUSDT", "matic": "MATICUSDT",
    "arbitrum": "ARBUSDT", "arb": "ARBUSDT",
    "optimism": "OPUSDT", "op": "OPUSDT",
    "sui": "SUIUSDT", "aptos": "APTUSDT", "apt": "APTUSDT",
    "near": "NEARUSDT", "injective": "INJUSDT", "inj": "INJUSDT",
}

TIMEFRAME_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h",
    "8h": "8h", "12h": "12h", "1d": "1d", "1w": "1w",
}

# ── Helpers ────────────────────────────────────────────────────────────────────

DATA_FRESHNESS = {}


def _fetch(url, timeout=15):
    """Fetch JSON from a URL. Returns dict or None on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Kairos/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"_error": str(e)}


def _fmt(val, suffix=""):
    """Format a number with commas, optional suffix."""
    if val is None:
        return "N/A"
    try:
        v = float(val)
        if v >= 1_000_000_000:
            return f"${v / 1_000_000_000:,.2f}B{suffix}"
        if v >= 1_000_000:
            return f"${v / 1_000_000:,.2f}M{suffix}"
        if v >= 1_000:
            return f"${v:,.0f}{suffix}"
        if v < 0.0001:
            return f"{v:.8f}"
        if v < 1:
            return f"{v:.6f}"
        return f"{v:,.4f}"
    except (ValueError, TypeError):
        return str(val)


def _pct(val):
    """Format a percentage."""
    if val is None:
        return "N/A"
    try:
        v = float(val)
        sign = "+" if v > 0 else ""
        return f"{sign}{v:.2f}%"
    except (ValueError, TypeError):
        return str(val)


def _stale_check(source, data, max_age_minutes=60):
    """Check if fetched data is stale based on timestamp field."""
    now = time.time()
    ts = None
    if isinstance(data, dict):
        ts = data.get("_fetched_at")
    if ts is None:
        return
    age_min = (now - ts) / 60
    if age_min > max_age_minutes:
        print(f"  [stale] WARNING: {source} data is {age_min:.0f} minutes old (max {max_age_minutes}m)", file=sys.stderr)


def _record_freshness(source):
    """Record when data was fetched for freshness tracking."""
    DATA_FRESHNESS[source] = time.time()


def fresh_since(source):
    """Get minutes since last data pull for a source."""
    if source in DATA_FRESHNESS:
        return f"{(time.time() - DATA_FRESHNESS[source]) / 60:.0f}"
    return "never"


# ── Data Fetchers ──────────────────────────────────────────────────────────────


@cached(ttl_seconds=180)
@retry(max_attempts=3, delay=2)
def binance_klines(symbol, interval="1h", limit=500):
    """Fetch kline/candlestick data from Binance."""
    url = f"{BINANCE_BASE}/klines?symbol={symbol}&interval={interval}&limit={limit}"
    data = _fetch(url)
    if not data or "_error" in data:
        return {"_error": f"Failed to fetch klines for {symbol}"}
    
    klines = []
    for k in data:
        try:
            klines.append({
                "open_time": k[0],
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "close_time": k[6],
                "quote_volume": float(k[7]),
                "count": k[8],
                "taker_buy_vol": float(k[9]),
                "taker_buy_quote": float(k[10]),
            })
        except (IndexError, ValueError, TypeError):
            continue
    
    _record_freshness(f"binance_{symbol}_{interval}")
    return {
        "symbol": symbol,
        "interval": interval,
        "count": len(klines),
        "klines": klines,
        "_fetched_at": time.time(),
    }


@cached(ttl_seconds=60)
@retry(max_attempts=3, delay=2)
def binance_ticker_24hr(symbol):
    """Fetch 24hr ticker stats from Binance."""
    url = f"{BINANCE_BASE}/ticker/24hr?symbol={symbol}"
    data = _fetch(url)
    if not data or "_error" in data:
        return {"_error": f"Failed to fetch ticker for {symbol}"}
    _record_freshness(f"ticker_{symbol}")
    return {
        "symbol": data.get("symbol"),
        "price_change": float(data.get("priceChange", 0)),
        "price_change_pct": float(data.get("priceChangePercent", 0)),
        "weighted_avg_price": float(data.get("weightedAvgPrice", 0)),
        "high": float(data.get("highPrice", 0)),
        "low": float(data.get("lowPrice", 0)),
        "volume": float(data.get("volume", 0)),
        "quote_volume": float(data.get("quoteVolume", 0)),
        "count": int(data.get("count", 0)),
        "_fetched_at": time.time(),
    }


@cached(ttl_seconds=60)
@retry(max_attempts=3, delay=2)
def binance_funding_rate(symbol, limit=100):
    """Fetch funding rate data from Binance Futures."""
    url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&limit={limit}"
    data = _fetch(url)
    if not data or "_error" in data:
        return {"_error": f"Failed to fetch funding rate for {symbol}"}
    
    rates = []
    for r in data:
        try:
            rates.append({
                "time": r.get("fundingTime"),
                "rate": float(r.get("fundingRate", 0)),
            })
        except (ValueError, TypeError):
            continue
    
    if rates:
        latest = rates[-1]["rate"]
        avg = sum(r["rate"] for r in rates) / len(rates)
        max_r = max(r["rate"] for r in rates)
        min_r = min(r["rate"] for r in rates)
    else:
        latest = avg = max_r = min_r = 0

    _record_freshness(f"funding_{symbol}")
    return {
        "symbol": symbol,
        "latest_rate": latest,
        "avg_rate": avg,
        "max_rate": max_r,
        "min_rate": min_r,
        "samples": len(rates),
        "_fetched_at": time.time(),
    }


@cached(ttl_seconds=60)
@retry(max_attempts=3, delay=2)
def binance_open_interest(symbol):
    """Fetch open interest from Binance Futures."""
    url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}"
    data = _fetch(url)
    if not data or "_error" in data:
        return {"_error": f"Failed to fetch open interest for {symbol}"}
    _record_freshness(f"oi_{symbol}")
    return {
        "symbol": data.get("symbol"),
        "open_interest": float(data.get("openInterest", 0)),
        "_fetched_at": time.time(),
    }


@cached(ttl_seconds=180)
@retry(max_attempts=3, delay=2)
def coin_price(coin_id):
    """Fetch current price and simple stats from CoinGecko."""
    url = f"{COINGECKO_BASE}/simple/price?ids={coin_id}&vs_currencies=usd&include_24hr_vol=true&include_24hr_change=true&include_market_cap=true"
    data = _fetch(url)
    if not data or coin_id not in data:
        return {"_error": f"No price data for {coin_id}"}
    d = data[coin_id]
    return {
        "price": d.get("usd"),
        "volume_24h": d.get("usd_24h_vol"),
        "change_24h": d.get("usd_24h_change"),
        "market_cap": d.get("usd_market_cap"),
    }


@cached(ttl_seconds=120)
@retry(max_attempts=3, delay=2)
def fear_greed_index():
    """Fetch Fear & Greed Index from alternative.me."""
    data = _fetch("https://api.alternative.me/fng/?limit=7")
    if not data or "data" not in data:
        return None
    today = data["data"][0]
    week_ago = data["data"][-1] if len(data["data"]) > 1 else None
    return {
        "value": today.get("value"),
        "classification": today.get("value_classification"),
        "trend": (f"{week_ago.get('value')} -> {today.get('value')}" if week_ago else None),
        "_fetched_at": time.time(),
    }


# ── Technical Indicator Calculations ───────────────────────────────────────────


def calc_rsi(prices, period=14):
    """Calculate RSI from a list of prices."""
    if not prices or len(prices) < period + 1:
        return None
    try:
        gains, losses = [], []
        for i in range(1, len(prices)):
            diff = prices[i] - prices[i - 1]
            gains.append(max(diff, 0))
            losses.append(max(-diff, 0))
        
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100.0 - (100.0 / (1.0 + rs)), 2)
    except (ZeroDivisionError, IndexError, ValueError):
        return None


def calc_macd(prices, fast=12, slow=26, signal=9):
    """Calculate MACD from a list of prices. Returns MACD line, signal line, histogram."""
    if not prices or len(prices) < slow + signal:
        return None
    
    def ema(data, period):
        multiplier = 2 / (period + 1)
        result = [data[0]]
        for i in range(1, len(data)):
            result.append((data[i] - result[-1]) * multiplier + result[-1])
        return result
    
    try:
        ema_fast = ema(prices, fast)
        ema_slow = ema(prices, slow)
        macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
        signal_line = ema(macd_line, signal)
        histogram = [m - s for m, s in zip(macd_line, signal_line)]
        
        return {
            "macd": round(macd_line[-1], 4),
            "signal": round(signal_line[-1], 4),
            "histogram": round(histogram[-1], 4),
            "histogram_prev": round(histogram[-2], 4) if len(histogram) > 1 else None,
        }
    except (IndexError, ValueError):
        return None


def calc_bollinger_bands(prices, period=20, std_dev=2):
    """Calculate Bollinger Bands."""
    if not prices or len(prices) < period:
        return None
    try:
        recent = prices[-period:]
        sma = sum(recent) / period
        variance = sum((p - sma) ** 2 for p in recent) / period
        std = variance ** 0.5
        return {
            "middle": round(sma, 4),
            "upper": round(sma + std_dev * std, 4),
            "lower": round(sma - std_dev * std, 4),
            "bandwidth": round((std_dev * std) / sma, 6) if sma != 0 else 0,
        }
    except (ZeroDivisionError, ValueError):
        return None


def calc_ema(prices, period=20):
    """Calculate EMA."""
    if not prices or len(prices) < period:
        return None
    try:
        multiplier = 2 / (period + 1)
        ema = sum(prices[:period]) / period
        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema
        return round(ema, 4)
    except (IndexError, ValueError):
        return None


def calc_sma(prices, period=20):
    """Calculate SMA."""
    if not prices or len(prices) < period:
        return None
    try:
        return round(sum(prices[-period:]) / period, 4)
    except (ZeroDivisionError, ValueError):
        return None


def calc_atr(klines, period=14):
    """Calculate ATR from kline data."""
    if not klines or len(klines) < period + 1:
        return None
    try:
        true_ranges = []
        for i in range(1, len(klines)):
            high = klines[i]["high"]
            low = klines[i]["low"]
            prev_close = klines[i - 1]["close"]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            true_ranges.append(tr)
        
        if len(true_ranges) < period:
            return None
        
        atr = sum(true_ranges[:period]) / period
        for tr in true_ranges[period:]:
            atr = (atr * (period - 1) + tr) / period
        
        return round(atr, 4)
    except (IndexError, ValueError, TypeError):
        return None


# ── Analysis Builder ───────────────────────────────────────────────────────────


def analyze_coin(coin, timeframe="1h", json_mode=False):
    """Build a comprehensive technical analysis report for a coin."""
    coin_id = COIN_IDS.get(coin.lower(), coin.lower())
    symbol = BINANCE_SYMBOLS.get(coin.lower())
    if not symbol:
        return {"_error": f"No Binance symbol mapping for '{coin}'. Try --list-coins"}
    
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    
    report = {
        "timestamp": timestamp,
        "coin": coin,
        "coin_id": coin_id,
        "symbol": symbol,
        "timeframe": timeframe,
    }
    
    # 1. Price data from CoinGecko
    price_data = coin_price(coin_id)
    report["price"] = price_data
    
    # 2. Klines from Binance
    kline_data = binance_klines(symbol, timeframe, limit=500)
    report["klines"] = kline_data
    
    # 3. Technical indicators
    indicators = {}
    if kline_data and "klines" in kline_data and kline_data["klines"]:
        closes = [k["close"] for k in kline_data["klines"]]
        highs = [k["high"] for k in kline_data["klines"]]
        lows = [k["low"] for k in kline_data["klines"]]
        
        indicators["rsi_14"] = calc_rsi(closes, 14)
        indicators["macd"] = calc_macd(closes)
        indicators["bb_20_2"] = calc_bollinger_bands(closes, 20, 2)
        indicators["ema_8"] = calc_ema(closes, 8)
        indicators["ema_21"] = calc_ema(closes, 21)
        indicators["ema_50"] = calc_ema(closes, 50)
        indicators["ema_200"] = calc_ema(closes, 200)
        indicators["sma_50"] = calc_sma(closes, 50)
        indicators["sma_200"] = calc_sma(closes, 200)
        indicators["atr_14"] = calc_atr(kline_data["klines"], 14)
        
        # Current price relative to EMAs
        current_price = closes[-1] if closes else 0
        ema_status = {}
        for ema_name in ["ema_8", "ema_21", "ema_50", "ema_200"]:
            val = indicators.get(ema_name)
            if val is not None and current_price:
                ema_status[f"{ema_name}_position"] = "above" if current_price > val else "below"
        indicators["ema_positions"] = ema_status
    
    report["indicators"] = indicators
    
    # 4. Market metrics (24h ticker)
    report["ticker_24h"] = binance_ticker_24hr(symbol)
    
    # 5. Funding rate (futures)
    report["funding_rate"] = binance_funding_rate(symbol)
    
    # 6. Open interest
    report["open_interest"] = binance_open_interest(symbol)
    
    # 7. Fear & Greed
    report["fear_greed"] = fear_greed_index()
    
    # 8. Freshness report
    report["data_freshness"] = {}
    for src in list(DATA_FRESHNESS.keys()):
        report["data_freshness"][src] = f"{fresh_since(src)}m"
    
    if json_mode:
        # Remove raw klines for cleaner JSON output in non-detailed mode
        if "klines" in report and report["klines"] and "klines" in report["klines"]:
            kc = len(report["klines"]["klines"])
            report["klines"] = {"count": kc, "interval": timeframe, "symbol": symbol}
            report["klines_note"] = f"{kc} candles available. Use --full to include raw data."
        return json.dumps(report, indent=2)
    
    return _format_human_report(report)


def _indicator_rsi_signal(rsi):
    """Get RSI signal interpretation."""
    if rsi is None:
        return "N/A"
    if rsi >= 70:
        return "Overbought"
    if rsi <= 30:
        return "Oversold"
    if rsi >= 60:
        return "Bullish bias"
    if rsi <= 40:
        return "Bearish bias"
    return "Neutral"


def _indicator_macd_signal(macd):
    """Get MACD signal interpretation."""
    if macd is None:
        return "N/A"
    hist = macd.get("histogram", 0)
    prev = macd.get("histogram_prev", 0)
    if hist > 0 and prev is not None and prev <= 0:
        return "Bullish crossover"
    if hist < 0 and prev is not None and prev >= 0:
        return "Bearish crossover"
    if hist > 0:
        return "Bullish (positive)"
    if hist < 0:
        return "Bearish (negative)"
    return "Neutral"


def _format_human_report(r):
    """Format the report for human reading."""
    lines = []
    lines.append(f"Kairos Data Report — {r['coin'].upper()} ({r['timeframe']})")
    lines.append(f"Generated: {r['timestamp']}")
    lines.append("")

    # Price
    px = r.get("price", {}) or {}
    lines.append("── Price ──")
    if "_error" not in px:
        lines.append(f"  Price            : {_fmt(px.get('price'))}")
        lines.append(f"  24h Change       : {_pct(px.get('change_24h'))}")
        lines.append(f"  24h Volume       : {_fmt(px.get('volume_24h'))}")
        lines.append(f"  Market Cap       : {_fmt(px.get('market_cap'))}")
    else:
        lines.append(f"  {px.get('_error')}")
    lines.append("")

    # Indicators
    ind = r.get("indicators", {}) or {}
    lines.append("── Technical Indicators ──")
    if ind:
        rsi = ind.get("rsi_14")
        lines.append(f"  RSI(14)          : {rsi} — {_indicator_rsi_signal(rsi)}")
        
        macd = ind.get("macd")
        if macd:
            lines.append(f"  MACD             : {macd.get('macd')} / Signal {macd.get('signal')} / Hist {macd.get('histogram')} ({_indicator_macd_signal(macd)})")
        
        bb = ind.get("bb_20_2")
        if bb:
            lines.append(f"  Bollinger(20,2)  : Upper {_fmt(bb.get('upper'))} / Mid {_fmt(bb.get('middle'))} / Lower {_fmt(bb.get('lower'))}")
            lines.append(f"  Bandwidth        : {bb.get('bandwidth', 0):.6f}")
        
        for ema_name in ["ema_8", "ema_21", "ema_50", "ema_200"]:
            val = ind.get(ema_name)
            if val is not None:
                pos = ind.get("ema_positions", {}).get(f"{ema_name}_position", "")
                pos_str = f" ({pos})" if pos else ""
                lines.append(f"  {ema_name.upper():16s}: {_fmt(val)}{pos_str}")
        
        atr = ind.get("atr_14")
        if atr:
            lines.append(f"  ATR(14)          : {_fmt(atr)}")
    else:
        lines.append("  (unavailable)")
    lines.append("")

    # 24h Ticker
    tk = r.get("ticker_24h", {}) or {}
    lines.append("── 24h Market Stats ──")
    if "_error" not in tk:
        lines.append(f"  High / Low       : {_fmt(tk.get('high'))} / {_fmt(tk.get('low'))}")
        lines.append(f"  Volume           : {_fmt(tk.get('volume'))}")
        lines.append(f"  Trades           : {tk.get('count', 'N/A')}")
    else:
        lines.append(f"  {tk.get('_error')}")
    lines.append("")

    # Funding Rate
    fr = r.get("funding_rate", {}) or {}
    lines.append("── Futures ──")
    if "_error" not in fr:
        latest_rate = fr.get("latest_rate", 0) * 100
        lines.append(f"  Funding Rate     : {latest_rate:.4f}%")
        lines.append(f"  8h Avg           : {fr.get('avg_rate', 0) * 100:.4f}%")
        lines.append(f"  Range            : {fr.get('min_rate', 0) * 100:.4f}% / {fr.get('max_rate', 0) * 100:.4f}%")
        if latest_rate > 0.05:
            lines.append("  *Cautions on longs (funding elevated)")
        elif latest_rate < -0.05:
            lines.append("  *Cautions on shorts (funding negative)")
    else:
        lines.append(f"  {fr.get('_error')}")

    # Open Interest
    oi = r.get("open_interest", {}) or {}
    if "_error" not in oi:
        lines.append(f"  Open Interest    : {_fmt(oi.get('open_interest'))}")
    lines.append("")

    # Fear & Greed
    fg = r.get("fear_greed")
    lines.append("── Sentiment ──")
    if fg:
        lines.append(f"  Fear & Greed     : {fg.get('value')} — {fg.get('classification')}")
    else:
        lines.append("  (unavailable)")
    lines.append("")

    # Data freshness
    df = r.get("data_freshness", {})
    if df:
        lines.append("── Data Freshness ──")
        for src, age in sorted(df.items()):
            lines.append(f"  {src:30s}: {age}")
        lines.append("")

    return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────────────────


def list_coins():
    """Print all known coin mappings."""
    print("Known Coin ID Mappings:")
    print(f"  {'Name':15s} {'CoinGecko':20s} {'Binance':15s}")
    print(f"  {'-'*15} {'-'*20} {'-'*15}")
    for name in sorted(COIN_IDS):
        cg = COIN_IDS.get(name, "?")
        bn = BINANCE_SYMBOLS.get(name, "?")
        print(f"  {name:15s} {cg:20s} {bn:15s}")
    print()
    print("Timeframes: " + ", ".join(sorted(TIMEFRAME_MAP.keys())))


def main():
    parser = argparse.ArgumentParser(
        description="Kairos — Technical Data Fetcher",
    )
    parser.add_argument("--coin", "-c", default="bitcoin",
                        help="Coin name or ID (default: bitcoin). See --list-coins.")
    parser.add_argument("--timeframe", "-t", default="1h", choices=list(TIMEFRAME_MAP.keys()),
                        help="Candle timeframe (default: 1h).")
    parser.add_argument("--json", "-j", action="store_true",
                        help="Output as JSON instead of human-readable report.")
    parser.add_argument("--list-coins", action="store_true",
                        help="List known coin ID mappings.")
    args = parser.parse_args()

    if args.list_coins:
        list_coins()
        return

    report = analyze_coin(coin=args.coin, timeframe=args.timeframe, json_mode=args.json)
    print(report)


if __name__ == "__main__":
    main()
