#!/usr/bin/env python3
"""
confluence_matrix.py — Multi-Timeframe Confluence Scoring for Kairos v3.0

Reads kline data across 4 timeframes and scores how many agree on direction.
Provides a confluence score (0-100) and top aligned timeframe pairs.

Usage:
    python3 confluence_matrix.py --coin bitcoin
    python3 confluence_matrix.py --coin ethereum --timeframes 1h,4h,1d,1w --json
    python3 confluence_matrix.py --list-timeframes

Output: confluence score 0-100, direction consensus, top aligned pairs.

Dependencies: python3 standard lib (urllib, json)
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

BINANCE_BASE = "https://api.binance.com/api/v3"

BINANCE_SYMBOLS = {
    "bitcoin": "BTCUSDT", "btc": "BTCUSDT",
    "ethereum": "ETHUSDT", "eth": "ETHUSDT",
    "solana": "SOLUSDT", "sol": "SOLUSDT",
    "cardano": "ADAUSDT", "ada": "ADAUSDT",
    "ripple": "XRPUSDT", "xrp": "XRPUSDT",
    "polkadot": "DOTUSDT", "dot": "DOTUSDT",
    "avalanche": "AVAXUSDT", "avax": "AVAXUSDT",
    "chainlink": "LINKUSDT", "link": "LINKUSDT",
    "arbitrum": "ARBUSDT", "arb": "ARBUSDT",
    "sui": "SUIUSDT", "aptos": "APTUSDT", "apt": "APTUSDT",
    "near": "NEARUSDT", "injective": "INJUSDT", "inj": "INJUSDT",
}

# Default timeframes to analyze (4 timeframes)
DEFAULT_TIMEFRAMES = ["15m", "1h", "4h", "1d"]

TIMEFRAME_LABELS = {
    "1m": "1 Minute",
    "5m": "5 Minutes",
    "15m": "15 Minutes",
    "30m": "30 Minutes",
    "1h": "1 Hour",
    "2h": "2 Hours",
    "4h": "4 Hours",
    "6h": "6 Hours",
    "8h": "8 Hours",
    "12h": "12 Hours",
    "1d": "1 Day",
    "1w": "1 Week",
}

# Factors that influence confluence weighting
# Higher timeframe = more weight
TIMEFRAME_WEIGHTS = {
    "1m": 1, "5m": 1, "15m": 2, "30m": 2,
    "1h": 3, "2h": 3, "4h": 4, "6h": 4,
    "8h": 4, "12h": 5, "1d": 5, "1w": 6,
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def _fetch(url, timeout=15):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Kairos/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"_error": str(e)}


def _fmt(val, suffix=""):
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


# ── Data Fetcher ───────────────────────────────────────────────────────────────


@cached(ttl_seconds=120)
@retry(max_attempts=3, delay=2)
def binance_klines(symbol, interval="1h", limit=200):
    """Fetch kline/candlestick data from Binance."""
    url = f"{BINANCE_BASE}/klines?symbol={symbol}&interval={interval}&limit={limit}"
    data = _fetch(url)
    if not data or "_error" in data:
        return {"_error": f"Failed to fetch klines for {symbol}"}
    
    klines = []
    for k in data:
        try:
            klines.append({
                "time": k[0],
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })
        except (IndexError, ValueError, TypeError):
            continue
    
    return {
        "symbol": symbol,
        "interval": interval,
        "count": len(klines),
        "klines": klines,
    }


# ── Technical Analysis Functions ───────────────────────────────────────────────


def analyze_timeframe(klines):
    """
    Analyze a single timeframe's klines and return directional signals.
    Returns dict with direction signals for this timeframe.
    """
    if not klines or len(klines) < 30:
        return {"_error": "Insufficient data"}
    
    closes = [k["close"] for k in klines]
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    current_price = closes[-1] if closes else 0
    
    signals = {}
    
    # 1. EMA trend (8 vs 21 vs 50)
    ema8 = _calc_ema(closes, 8)
    ema21 = _calc_ema(closes, 21)
    ema50 = _calc_ema(closes, 50)
    ema200 = _calc_ema(closes, 200)
    
    trend_bullish = 0
    trend_bearish = 0
    
    if ema8 and ema21 and ema50:
        if ema8 > ema21 > ema50:
            trend_bullish += 1
        elif ema8 < ema21 < ema50:
            trend_bearish += 1
        # Price vs EMAs
        if ema8 and current_price > ema8:
            trend_bullish += 1
        else:
            trend_bearish += 1
        if ema21 and current_price > ema21:
            trend_bullish += 1
        else:
            trend_bearish += 1
        if ema50 and current_price > ema50:
            trend_bullish += 1
        else:
            trend_bearish += 1
    
    if trend_bullish > trend_bearish:
        signals["ema_trend"] = "bullish"
    elif trend_bearish > trend_bullish:
        signals["ema_trend"] = "bearish"
    else:
        signals["ema_trend"] = "neutral"
    
    signals["ema_8"] = ema8
    signals["ema_21"] = ema21
    signals["ema_50"] = ema50
    signals["ema_200"] = ema200
    
    # 2. RSI
    rsi = _calc_rsi(closes, 14)
    signals["rsi_14"] = rsi
    if rsi is not None:
        if rsi > 60:
            signals["rsi_signal"] = "bullish"
        elif rsi < 40:
            signals["rsi_signal"] = "bearish"
        else:
            signals["rsi_signal"] = "neutral"
    else:
        signals["rsi_signal"] = "neutral"
    
    # 3. MACD
    macd = _calc_macd(closes)
    signals["macd"] = macd
    if macd:
        signals["macd_signal"] = "bullish" if macd.get("histogram", 0) > 0 else "bearish"
    else:
        signals["macd_signal"] = "neutral"
    
    # 4. Bollinger Bands position
    bb = _calc_bollinger(closes)
    signals["bollinger"] = bb
    if bb:
        if current_price > bb["upper"]:
            signals["bb_signal"] = "overbought"
        elif current_price < bb["lower"]:
            signals["bb_signal"] = "oversold"
        else:
            signals["bb_signal"] = "neutral"
    else:
        signals["bb_signal"] = "neutral"
    
    # 5. Price action: higher highs / higher lows?
    if len(closes) >= 20:
        recent_highs = [max(klines[i]["high"], klines[i+1]["high"]) for i in range(-10, -1)]
        recent_lows = [min(klines[i]["low"], klines[i+1]["low"]) for i in range(-10, -1)]
        
        # Simple: compare recent 5 candles to prior 5
        if len(closes) >= 10:
            recent = closes[-5:]
            prior = closes[-10:-5]
            if sum(recent) / len(recent) > sum(prior) / len(prior):
                signals["pa_trend"] = "bullish"
            else:
                signals["pa_trend"] = "bearish"
    else:
        signals["pa_trend"] = "neutral"
    
    # 6. Overall direction for this timeframe
    bullish_count = sum(1 for s in [signals.get("ema_trend"), signals.get("rsi_signal"),
                                     signals.get("macd_signal"), signals.get("pa_trend")]
                        if s == "bullish")
    bearish_count = sum(1 for s in [signals.get("ema_trend"), signals.get("rsi_signal"),
                                     signals.get("macd_signal"), signals.get("pa_trend")]
                        if s == "bearish")
    
    if bullish_count > bearish_count:
        signals["direction"] = "bullish"
        signals["strength"] = int((bullish_count / 4) * 100)
    elif bearish_count > bullish_count:
        signals["direction"] = "bearish"
        signals["strength"] = int((bearish_count / 4) * 100)
    else:
        signals["direction"] = "neutral"
        signals["strength"] = 50
    
    return signals


def _calc_ema(prices, period):
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


def _calc_rsi(prices, period=14):
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


def _calc_macd(prices, fast=12, slow=26, signal=9):
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
        }
    except (IndexError, ValueError):
        return None


def _calc_bollinger(prices, period=20, std_dev=2):
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
        }
    except (ZeroDivisionError, ValueError):
        return None


# ── Confluence Scoring ─────────────────────────────────────────────────────────


def score_confluence(timeframe_results):
    """
    Score confluence across multiple timeframes.
    
    Returns:
    - confluence_score: 0-100 (how many timeframes agree)
    - consensus_direction: the direction most timeframes agree on
    - top_pairs: highest-conviction timeframe pairs
    """
    if not timeframe_results:
        return {
            "confluence_score": 0,
            "consensus_direction": "neutral",
            "agreement_ratio": "0/0",
            "top_pairs": [],
        }
    
    bullish_count = 0
    bearish_count = 0
    neutral_count = 0
    total_weight = 0
    weighted_bullish = 0
    weighted_bearish = 0
    
    tf_details = []
    
    for tf_name, analysis in timeframe_results.items():
        if "_error" in analysis:
            continue
        
        direction = analysis.get("direction", "neutral")
        strength = analysis.get("strength", 50)
        weight = TIMEFRAME_WEIGHTS.get(tf_name, 3)
        
        if direction == "bullish":
            bullish_count += 1
            weighted_bullish += weight * (strength / 100)
        elif direction == "bearish":
            bearish_count += 1
            weighted_bearish += weight * (strength / 100)
        else:
            neutral_count += 1
        
        total_weight += weight
        
        tf_details.append({
            "timeframe": tf_name,
            "label": TIMEFRAME_LABELS.get(tf_name, tf_name),
            "direction": direction,
            "strength": strength,
            "weight": weight,
        })
    
    total_tfs = len(tf_details)
    if total_tfs == 0:
        return {"confluence_score": 0, "consensus_direction": "neutral", "agreement_ratio": "0/0", "top_pairs": []}
    
    # Consensus direction
    if bullish_count > bearish_count and bullish_count >= neutral_count:
        consensus = "bullish"
        agreement = bullish_count
    elif bearish_count > bullish_count and bearish_count >= neutral_count:
        consensus = "bearish"
        agreement = bearish_count
    else:
        consensus = "neutral"
        agreement = neutral_count
    
    # Confluence score: weighted agreement ratio * 100
    if total_weight > 0:
        weighted_agreement = (weighted_bullish if consensus == "bullish" else weighted_bearish) / total_weight
        # Also factor in raw agreement ratio
        raw_ratio = agreement / total_tfs
        confluence_score = int((weighted_agreement * 0.6 + raw_ratio * 0.4) * 100)
    else:
        confluence_score = int((agreement / total_tfs) * 100)
    
    # Top aligned pairs: find pairs of timeframes that agree
    top_pairs = []
    bullish_tfs = [t for t in tf_details if t["direction"] == "bullish"]
    bearish_tfs = [t for t in tf_details if t["direction"] == "bearish"]
    
    if consensus == "bullish" and len(bullish_tfs) >= 2:
        # Sort by combined weight * strength
        pairs = []
        for i in range(len(bullish_tfs)):
            for j in range(i + 1, len(bullish_tfs)):
                combined = (bullish_tfs[i]["weight"] + bullish_tfs[j]["weight"]) * \
                           (bullish_tfs[i]["strength"] + bullish_tfs[j]["strength"]) / 2
                pairs.append({
                    "pair": f"{bullish_tfs[i]['label']} + {bullish_tfs[j]['label']}",
                    "direction": "bullish",
                    "combined_strength": int(combined),
                })
        pairs.sort(key=lambda x: x["combined_strength"], reverse=True)
        top_pairs = pairs[:3]
    
    elif consensus == "bearish" and len(bearish_tfs) >= 2:
        pairs = []
        for i in range(len(bearish_tfs)):
            for j in range(i + 1, len(bearish_tfs)):
                combined = (bearish_tfs[i]["weight"] + bearish_tfs[j]["weight"]) * \
                           (bearish_tfs[i]["strength"] + bearish_tfs[j]["strength"]) / 2
                pairs.append({
                    "pair": f"{bearish_tfs[i]['label']} + {bearish_tfs[j]['label']}",
                    "direction": "bearish",
                    "combined_strength": int(combined),
                })
        pairs.sort(key=lambda x: x["combined_strength"], reverse=True)
        top_pairs = pairs[:3]
    
    return {
        "confluence_score": min(confluence_score, 100),
        "consensus_direction": consensus,
        "agreement_ratio": f"{agreement}/{total_tfs}",
        "bullish_tfs": bullish_count,
        "bearish_tfs": bearish_count,
        "neutral_tfs": neutral_count,
        "top_pairs": top_pairs,
        "timeframe_details": tf_details,
    }


# ── Report Builder ─────────────────────────────────────────────────────────────


def build_confluence_report(coin, timeframes=None, json_mode=False):
    """Build multi-timeframe confluence scoring report."""
    if timeframes is None:
        timeframes = DEFAULT_TIMEFRAMES
    
    symbol = BINANCE_SYMBOLS.get(coin.lower())
    if not symbol:
        return {"_error": f"No Binance symbol mapping for '{coin}'"}
    
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    
    # Fetch klines for each timeframe
    timeframe_results = {}
    for tf in timeframes:
        kline_data = binance_klines(symbol, tf, limit=200)
        if "_error" in kline_data:
            timeframe_results[tf] = {"_error": kline_data["_error"]}
            continue
        
        klines = kline_data.get("klines", [])
        analysis = analyze_timeframe(klines)
        timeframe_results[tf] = analysis
    
    # Get current price from highest timeframe
    primary_tf = timeframes[-1]  # Highest timeframe
    primary_data = timeframe_results.get(primary_tf, {})
    
    report = {
        "timestamp": timestamp,
        "coin": coin,
        "symbol": symbol,
        "version": "3.0",
        "timeframes_analyzed": timeframes,
    }
    
    # Add confluence scoring
    report["confluence"] = score_confluence(timeframe_results)
    
    if json_mode:
        return json.dumps(report, indent=2)
    
    return _format_human_report(report)


def _format_human_report(r):
    """Format confluence report for human reading."""
    lines = []
    lines.append(f"Kairos Confluence Matrix — {r['coin'].upper()} (v3.0)")
    lines.append(f"Generated: {r['timestamp']}")
    lines.append(f"Timeframes: {', '.join(r.get('timeframes_analyzed', []))}")
    lines.append("")
    
    conf = r.get("confluence", {})
    
    # Score bar
    score = conf.get("confluence_score", 0)
    bar = "▓" * (score // 10) + "░" * (10 - score // 10)
    direction = conf.get("consensus_direction", "neutral").upper()
    
    lines.append(f"══ CONFLUENCE SCORE: {score}/100 [{bar}] ══")
    lines.append(f"  Consensus Direction : {direction}")
    lines.append(f"  Agreement Ratio     : {conf.get('agreement_ratio', 'N/A')}")
    lines.append(f"  Bullish TFs         : {conf.get('bullish_tfs', 0)}")
    lines.append(f"  Bearish TFs         : {conf.get('bearish_tfs', 0)}")
    lines.append(f"  Neutral TFs         : {conf.get('neutral_tfs', 0)}")
    lines.append("")
    
    # Interpretation
    score_val = conf.get("confluence_score", 0)
    if score_val >= 75:
        lines.append("  INTERPRETATION: Strong confluence — high-confidence setup")
    elif score_val >= 50:
        lines.append("  INTERPRETATION: Moderate confluence — bias exists, manage risk")
    elif score_val >= 30:
        lines.append("  INTERPRETATION: Weak confluence — mixed signals, stay neutral")
    else:
        lines.append("  INTERPRETATION: No confluence — timeframes disagree, avoid trading")
    lines.append("")
    
    # Top aligned pairs
    top_pairs = conf.get("top_pairs", [])
    if top_pairs:
        lines.append("── Top Aligned Timeframe Pairs ──")
        for pair in top_pairs:
            lines.append(f"  {pair.get('pair', '?')} — strength {pair.get('combined_strength', 0)} ({pair.get('direction', '?')})")
        lines.append("")
    
    # Per-timeframe breakdown
    tf_details = conf.get("timeframe_details", [])
    if tf_details:
        lines.append("── Per Timeframe Breakdown ──")
        lines.append(f"  {'TF':10s} {'Dir':10s} {'Str':5s} {'Wt':3s}  {'EMAs':12s} {'RSI':8s} {'MACD':8s} {'BB':10s}")
        lines.append(f"  {'-'*10} {'-'*10} {'-'*5} {'-'*3}  {'-'*12} {'-'*8} {'-'*8} {'-'*10}")
        
        for tf in tf_details:
            tf_name = tf.get("timeframe", "?")
            tf_dir = tf.get("direction", "?")[:8]
            tf_str = f"{tf.get('strength', 0)}%"
            tf_wt = str(tf.get("weight", 0))
            lines.append(f"  {tf_name:10s} {tf_dir:10s} {tf_str:5s} {tf_wt:3s}")
        
        lines.append("")
    
    return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────────────────


def list_timeframes():
    """List available timeframes for confluence analysis."""
    print("Available Timeframes for Confluence Analysis:")
    print()
    for key, label in sorted(TIMEFRAME_LABELS.items()):
        weight = TIMEFRAME_WEIGHTS.get(key, 3)
        print(f"  {key:5s}  {label:15s}  weight: {weight}")
    print()
    print("Default 4 timeframes: " + ", ".join(DEFAULT_TIMEFRAMES))
    print("Pass custom timeframes with --timeframes (comma-separated)")


def main():
    parser = argparse.ArgumentParser(
        description="Kairos — Multi-Timeframe Confluence Matrix",
    )
    parser.add_argument("--coin", "-c", default="bitcoin",
                        help="Coin name or ID (default: bitcoin).")
    parser.add_argument("--timeframes", "-t",
                        default=",".join(DEFAULT_TIMEFRAMES),
                        help=f"Comma-separated timeframes (default: {','.join(DEFAULT_TIMEFRAMES)}).")
    parser.add_argument("--json", "-j", action="store_true",
                        help="Output as JSON.")
    parser.add_argument("--list-timeframes", action="store_true",
                        help="List available timeframes.")
    args = parser.parse_args()

    if args.list_timeframes:
        list_timeframes()
        return

    tfs = [tf.strip() for tf in args.timeframes.split(",") if tf.strip()]
    report = build_confluence_report(coin=args.coin, timeframes=tfs, json_mode=args.json)
    print(report)


if __name__ == "__main__":
    main()
