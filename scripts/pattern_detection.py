#!/usr/bin/env python3
"""
pattern_detection.py — Chart Pattern Detection for Kairos v3.0

Programmatic detection of chart patterns from kline data:
- Flags, Pennants, Wedges (rising/falling)
- Head and Shoulders (top/bottom)
- Double Tops / Double Bottoms
- Trendline breakouts

Uses linear regression for trendline detection.

Usage:
    python3 pattern_detection.py --coin bitcoin
    python3 pattern_detection.py --coin ethereum --timeframe 4h --json
    python3 pattern_detection.py --list-patterns

Output: pattern type, confidence (0-100), target price, invalidation level.

Dependencies: python3 standard lib (urllib, json)
"""

import argparse
import functools
import json
import math
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

TIMEFRAME_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h",
    "8h": "8h", "12h": "12h", "1d": "1d", "1w": "1w",
}

# Minimum number of candles needed for pattern detection
MIN_CANDLES = 50

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


def _linear_regression(points):
    """
    Perform linear regression on a list of (x, y) points.
    Returns (slope, intercept, r_squared).
    """
    n = len(points)
    if n < 2:
        return 0, 0, 0
    
    sum_x = sum(p[0] for p in points)
    sum_y = sum(p[1] for p in points)
    sum_xy = sum(p[0] * p[1] for p in points)
    sum_xx = sum(p[0] ** 2 for p in points)
    
    denominator = n * sum_xx - sum_x ** 2
    if denominator == 0:
        return 0, 0, 0
    
    slope = (n * sum_xy - sum_x * sum_y) / denominator
    intercept = (sum_y - slope * sum_x) / n
    
    # R-squared
    mean_y = sum_y / n
    ss_tot = sum((p[1] - mean_y) ** 2 for p in points)
    ss_res = sum((p[1] - (slope * p[0] + intercept)) ** 2 for p in points)
    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0
    
    return slope, intercept, r_squared


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
        "_fetched_at": time.time(),
    }


# ── Pattern Detection ──────────────────────────────────────────────────────────


def find_peaks_troughs(klines, window=5):
    """
    Identify swing highs (peaks) and swing lows (troughs) in price data.
    Uses a simple rolling window approach.
    Returns lists of (index, price) tuples for peaks and troughs.
    """
    highs = [(i, k["high"]) for i, k in enumerate(klines)]
    lows = [(i, k["low"]) for i, k in enumerate(klines)]
    
    peaks = []
    troughs = []
    
    half_window = window // 2
    for i in range(half_window, len(klines) - half_window):
        # Check if this is a swing high
        is_peak = True
        for j in range(i - half_window, i + half_window + 1):
            if j != i and highs[j][1] >= highs[i][1]:
                is_peak = False
                break
        if is_peak:
            peaks.append(highs[i])
        
        # Check if this is a swing low
        is_trough = True
        for j in range(i - half_window, i + half_window + 1):
            if j != i and lows[j][1] <= lows[i][1]:
                is_trough = False
                break
        if is_trough:
            troughs.append(lows[i])
    
    return peaks, troughs


def detect_trendlines(klines):
    """
    Detect trendlines using linear regression on peaks and troughs.
    Returns ascending, descending, and horizontal trendlines.
    """
    peaks, troughs = find_peaks_troughs(klines, window=5)
    
    results = []
    
    # Detect ascending trendline (rising lows)
    if len(troughs) >= 3:
        slope, intercept, r2 = _linear_regression(troughs)
        if slope > 0 and r2 > 0.7:
            results.append({
                "type": "ascending_trendline",
                "slope": round(slope, 6),
                "r_squared": round(r2, 4),
                "strength": "strong" if r2 > 0.85 else "moderate",
                "description": f"Ascending support line (rising lows)",
            })
    
    # Detect descending trendline (falling highs)
    if len(peaks) >= 3:
        slope, intercept, r2 = _linear_regression(peaks)
        if slope < 0 and r2 > 0.7:
            results.append({
                "type": "descending_trendline",
                "slope": round(slope, 6),
                "r_squared": round(r2, 4),
                "strength": "strong" if r2 > 0.85 else "moderate",
                "description": f"Descending resistance line (falling highs)",
            })
    
    return results


def detect_double_top_bottom(klines, lookback=40):
    """
    Detect double top and double bottom patterns.
    A double top: two peaks of similar height with a trough between them.
    A double bottom: two troughs of similar depth with a peak between them.
    """
    if len(klines) < lookback:
        return []
    
    recent = klines[-lookback:]
    peaks, troughs = find_peaks_troughs(recent, window=4)
    
    results = []
    
    # Double Top: two peaks of similar height
    if len(peaks) >= 2:
        for i in range(len(peaks) - 1):
            p1_idx, p1_price = peaks[i]
            p2_idx, p2_price = peaks[i + 1]
            
            # Peaks should be within ~3% of each other
            price_diff = abs(p1_price - p2_price) / max(p1_price, p2_price)
            if price_diff < 0.03:
                # Find the trough between them
                trough_between = [t for t in troughs if p1_idx < t[0] < p2_idx]
                if trough_between:
                    lowest_trough = min(trough_between, key=lambda x: x[1])
                    neckline = lowest_trough[1]
                    target = neckline - (p1_price - neckline)  # Measured move down
                    confidence = min(80, int((1 - price_diff / 0.03) * 80))
                    
                    results.append({
                        "pattern": "double_top",
                        "direction": "short",
                        "peak1_price": p1_price,
                        "peak2_price": p2_price,
                        "neckline": neckline,
                        "target": target,
                        "invalidation": max(p1_price, p2_price) * 1.01,  # Above either peak
                        "confidence": confidence,
                        "description": f"Double top at ${_fmt(p1_price)} and ${_fmt(p2_price)}, neckline ${_fmt(neckline)}",
                    })
    
    # Double Bottom: two troughs of similar depth
    if len(troughs) >= 2:
        for i in range(len(troughs) - 1):
            t1_idx, t1_price = troughs[i]
            t2_idx, t2_price = troughs[i + 1]
            
            price_diff = abs(t1_price - t2_price) / max(t1_price, t2_price)
            if price_diff < 0.03:
                peak_between = [p for p in peaks if t1_idx < p[0] < t2_idx]
                if peak_between:
                    highest_peak = max(peak_between, key=lambda x: x[1])
                    neckline = highest_peak[1]
                    target = neckline + (neckline - t1_price)
                    confidence = min(80, int((1 - price_diff / 0.03) * 80))
                    
                    results.append({
                        "pattern": "double_bottom",
                        "direction": "long",
                        "trough1_price": t1_price,
                        "trough2_price": t2_price,
                        "neckline": neckline,
                        "target": target,
                        "invalidation": min(t1_price, t2_price) * 0.99,  # Below either trough
                        "confidence": confidence,
                        "description": f"Double bottom at ${_fmt(t1_price)} and ${_fmt(t2_price)}, neckline ${_fmt(neckline)}",
                    })
    
    return results


def detect_head_and_shoulders(klines, lookback=60):
    """
    Detect head and shoulders (top) and inverse head and shoulders (bottom).
    
    Head and Shoulders Top: left shoulder, head (higher), right shoulder (same height as left)
    Inverse H&S: left shoulder, head (lower), right shoulder
    """
    if len(klines) < lookback:
        return []
    
    recent = klines[-lookback:]
    peaks, troughs = find_peaks_troughs(recent, window=5)
    
    results = []
    
    # Need at least 3 peaks for H&S top, 3 troughs for inverse
    if len(peaks) >= 3:
        for i in range(len(peaks) - 2):
            left = peaks[i]
            head = peaks[i + 1]
            right = peaks[i + 2]
            
            # Head must be higher than both shoulders
            if head[1] > left[1] and head[1] > right[1]:
                # Shoulders should be roughly similar height (within 5%)
                shoulder_diff = abs(left[1] - right[1]) / max(left[1], right[1])
                if shoulder_diff < 0.05:
                    # Find neckline (trough between left+head and head+right)
                    trough_between_left_head = [t for t in troughs if left[0] < t[0] < head[0]]
                    trough_between_head_right = [t for t in troughs if head[0] < t[0] < right[0]]
                    
                    if trough_between_left_head and trough_between_head_right:
                        neckline_left = min(trough_between_left_head, key=lambda x: x[1])
                        neckline_right = min(trough_between_head_right, key=lambda x: x[1])
                        avg_neckline = (neckline_left[1] + neckline_right[1]) / 2
                        
                        # Measured move: head height above neckline projected down
                        head_height = head[1] - avg_neckline
                        target = avg_neckline - head_height
                        
                        confidence = min(75, int((1 - shoulder_diff / 0.05) * 75))
                        
                        results.append({
                            "pattern": "head_and_shoulders_top",
                            "direction": "short",
                            "left_shoulder": left[1],
                            "head": head[1],
                            "right_shoulder": right[1],
                            "neckline": avg_neckline,
                            "target": target,
                            "invalidation": head[1] * 1.01,
                            "confidence": confidence,
                            "description": f"H&S top: left ${_fmt(left[1])}, head ${_fmt(head[1])}, right ${_fmt(right[1])}, neckline ${_fmt(avg_neckline)}",
                        })
    
    # Inverse H&S (bottom)
    if len(troughs) >= 3:
        for i in range(len(troughs) - 2):
            left = troughs[i]
            head = troughs[i + 1]
            right = troughs[i + 2]
            
            if head[1] < left[1] and head[1] < right[1]:
                shoulder_diff = abs(left[1] - right[1]) / max(left[1], right[1])
                if shoulder_diff < 0.05:
                    peak_between_left_head = [p for p in peaks if left[0] < p[0] < head[0]]
                    peak_between_head_right = [p for p in peaks if head[0] < p[0] < right[0]]
                    
                    if peak_between_left_head and peak_between_head_right:
                        neckline_left = max(peak_between_left_head, key=lambda x: x[1])
                        neckline_right = max(peak_between_head_right, key=lambda x: x[1])
                        avg_neckline = (neckline_left[1] + neckline_right[1]) / 2
                        
                        head_depth = avg_neckline - head[1]
                        target = avg_neckline + head_depth
                        
                        confidence = min(75, int((1 - shoulder_diff / 0.05) * 75))
                        
                        results.append({
                            "pattern": "inverse_head_and_shoulders",
                            "direction": "long",
                            "left_shoulder": left[1],
                            "head": head[1],
                            "right_shoulder": right[1],
                            "neckline": avg_neckline,
                            "target": target,
                            "invalidation": head[1] * 0.99,
                            "confidence": confidence,
                            "description": f"Inverse H&S: left ${_fmt(left[1])}, head ${_fmt(head[1])}, right ${_fmt(right[1])}, neckline ${_fmt(avg_neckline)}",
                        })
    
    return results


def detect_flag_pennant(klines, lookback=30):
    """
    Detect flags and pennants.
    Flag: Rectangular consolidation after a sharp move.
    Pennant: Triangular consolidation (converging trendlines) after a sharp move.
    """
    if len(klines) < lookback:
        return []
    
    recent = klines[-lookback:]
    if len(recent) < 20:
        return []
    
    results = []
    
    # Get the first 1/3 and last 2/3 of the lookback period
    pole_end = lookback // 3
    pole_klines = recent[:pole_end]
    flag_klines = recent[pole_end:]
    
    if len(pole_klines) < 5 or len(flag_klines) < 10:
        return []
    
    # Determine the pole direction (sharp move)
    pole_start_price = pole_klines[0]["close"]
    pole_end_price = pole_klines[-1]["close"]
    pole_move = (pole_end_price - pole_start_price) / pole_start_price * 100
    
    # Need at least 5% pole move
    if abs(pole_move) < 5:
        return []
    
    pole_direction = "up" if pole_move > 0 else "down"
    
    # Analyze flag/pennant consolidation
    flag_highs = [k["high"] for k in flag_klines]
    flag_lows = [k["low"] for k in flag_klines]
    
    flag_highs_points = [(i, h) for i, h in enumerate(flag_highs)]
    flag_lows_points = [(i, h) for i, h in enumerate(flag_lows)]
    
    if len(flag_highs_points) < 3 or len(flag_lows_points) < 3:
        return []
    
    h_slope, h_intercept, h_r2 = _linear_regression(flag_highs_points)
    l_slope, l_intercept, l_r2 = _linear_regression(flag_lows_points)
    
    # Flag: relatively flat trendlines (slopes near 0)
    # Pennant: converging trendlines (highs sloping down, lows sloping up in up-trend)
    
    if pole_direction == "up":
        # After upward pole
        if abs(h_slope) < 0.5 and abs(l_slope) < 0.5:
            # Flag
            pole_height = abs(pole_end_price - pole_start_price)
            target = flag_klines[-1]["close"] + pole_height * 0.5  # Conservative target: 50% of pole
            results.append({
                "pattern": "bull_flag",
                "direction": "long",
                "pole_move_pct": round(pole_move, 1),
                "target": target,
                "invalidation": min(flag_lows) * 0.98,
                "confidence": 60,
                "description": f"Bull flag after {pole_move:.1f}% upward pole",
            })
        elif h_slope < -0.3 and l_slope > 0.3:
            # Pennant (converging)
            pole_height = abs(pole_end_price - pole_start_price)
            target = flag_klines[-1]["close"] + pole_height * 0.5
            results.append({
                "pattern": "bull_pennant",
                "direction": "long",
                "pole_move_pct": round(pole_move, 1),
                "target": target,
                "invalidation": min(flag_lows) * 0.97,
                "confidence": 65,
                "description": f"Bull pennant after {pole_move:.1f}% upward move",
            })
    else:
        # After downward pole
        if abs(h_slope) < 0.5 and abs(l_slope) < 0.5:
            pole_height = abs(pole_end_price - pole_start_price)
            target = flag_klines[-1]["close"] - pole_height * 0.5
            results.append({
                "pattern": "bear_flag",
                "direction": "short",
                "pole_move_pct": round(pole_move, 1),
                "target": target,
                "invalidation": max(flag_highs) * 1.02,
                "confidence": 60,
                "description": f"Bear flag after {abs(pole_move):.1f}% downward pole",
            })
        elif h_slope > -0.3 and l_slope < 0.3:
            # In a downtrend pennant, highs are relatively flat, lows sloping down
            pole_height = abs(pole_end_price - pole_start_price)
            target = flag_klines[-1]["close"] - pole_height * 0.5
            results.append({
                "pattern": "bear_pennant",
                "direction": "short",
                "pole_move_pct": round(pole_move, 1),
                "target": target,
                "invalidation": max(flag_highs) * 1.03,
                "confidence": 65,
                "description": f"Bear pennant after {abs(pole_move):.1f}% downward move",
            })
    
    return results


def detect_wedges(klines, lookback=40):
    """
    Detect rising and falling wedges.
    Rising wedge: higher highs and higher lows, but highs rising faster -> bearish reversal.
    Falling wedge: lower highs and lower lows, but lows falling slower -> bullish reversal.
    """
    if len(klines) < lookback:
        return []
    
    recent = klines[-lookback:]
    peaks, troughs = find_peaks_troughs(recent, window=4)
    
    results = []
    
    if len(peaks) >= 3 and len(troughs) >= 3:
        # Use 5-7 most recent peaks and troughs
        recent_peaks = peaks[-5:]
        recent_troughs = troughs[-5:]
        
        if len(recent_peaks) >= 3 and len(recent_troughs) >= 3:
            p_slope, p_int, p_r2 = _linear_regression(recent_peaks)
            t_slope, t_int, t_r2 = _linear_regression(recent_troughs)
            
            # Rising wedge: both slopes positive, peaks steeper than troughs
            if p_slope > 0 and t_slope > 0 and p_slope > t_slope and p_r2 > 0.5 and t_r2 > 0.5:
                current_price = recent[-1]["close"]
                apex_idx = (t_int - p_int) / (p_slope - t_slope) if (p_slope - t_slope) != 0 else len(recent)
                apex_price = p_slope * apex_idx + p_int
                
                results.append({
                    "pattern": "rising_wedge",
                    "direction": "short",
                    "peak_slope": round(p_slope, 4),
                    "trough_slope": round(t_slope, 4),
                    "apex_index": int(apex_idx),
                    "confidence": 55,
                    "invalidation": max(p[1] for p in recent_peaks) * 1.02,
                    "description": f"Rising wedge — bearish reversal pattern (peaks steeper than troughs)",
                })
            
            # Falling wedge: both slopes negative, troughs steeper (more negative) than peaks
            elif p_slope < 0 and t_slope < 0 and t_slope < p_slope and p_r2 > 0.5 and t_r2 > 0.5:
                results.append({
                    "pattern": "falling_wedge",
                    "direction": "long",
                    "peak_slope": round(p_slope, 4),
                    "trough_slope": round(t_slope, 4),
                    "confidence": 55,
                    "invalidation": min(t[1] for t in recent_troughs) * 0.98,
                    "description": f"Falling wedge — bullish reversal pattern (troughs steeper than peaks)",
                })
    
    return results


def detect_all_patterns(klines):
    """Run all pattern detection algorithms and return consolidated results."""
    if not klines or len(klines) < MIN_CANDLES:
        return {"_error": f"Need at least {MIN_CANDLES} candles, got {len(klines) if klines else 0}"}
    
    all_patterns = []
    
    # Trendlines
    trendlines = detect_trendlines(klines)
    all_patterns.extend(trendlines)
    
    # Double tops/bottoms
    double_patterns = detect_double_top_bottom(klines)
    all_patterns.extend(double_patterns)
    
    # Head and shoulders
    hs_patterns = detect_head_and_shoulders(klines)
    all_patterns.extend(hs_patterns)
    
    # Flags and pennants
    flag_patterns = detect_flag_pennant(klines)
    all_patterns.extend(flag_patterns)
    
    # Wedges
    wedge_patterns = detect_wedges(klines)
    all_patterns.extend(wedge_patterns)
    
    return {
        "total_patterns": len(all_patterns),
        "patterns": all_patterns,
        "detected_at": time.time(),
    }


# ── Report Builder ─────────────────────────────────────────────────────────────


def build_pattern_report(coin, timeframe="1h", json_mode=False):
    """Build pattern detection report for a coin on a given timeframe."""
    symbol = BINANCE_SYMBOLS.get(coin.lower())
    if not symbol:
        return {"_error": f"No Binance symbol mapping for '{coin}'"}
    
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    
    # Fetch kline data
    kline_data = binance_klines(symbol, timeframe, limit=200)
    if "_error" in kline_data:
        return kline_data
    
    klines = kline_data.get("klines", [])
    
    # Run pattern detection
    pattern_results = detect_all_patterns(klines)
    
    # Get current price
    current_price = klines[-1]["close"] if klines else None
    
    report = {
        "timestamp": timestamp,
        "coin": coin,
        "symbol": symbol,
        "timeframe": timeframe,
        "version": "3.0",
        "current_price": current_price,
        "candles_analyzed": len(klines),
    }
    
    report.update(pattern_results)
    
    if json_mode:
        return json.dumps(report, indent=2)
    
    return _format_human_report(report)


def _format_human_report(r):
    """Format pattern report for human reading."""
    lines = []
    lines.append(f"Kairos Pattern Detection — {r['coin'].upper()} ({r['timeframe']}) (v3.0)")
    lines.append(f"Generated: {r['timestamp']}")
    lines.append(f"Current Price: {_fmt(r.get('current_price'))}")
    lines.append(f"Candles Analyzed: {r.get('candles_analyzed', 0)}")
    lines.append("")
    
    patterns = r.get("patterns", [])
    if not patterns:
        lines.append("No patterns detected.")
        lines.append("")
        return "\n".join(lines)
    
    lines.append(f"── Patterns Found: {r.get('total_patterns', 0)} ──")
    lines.append("")
    
    # Trading patterns (with direction)
    trade_patterns = [p for p in patterns if "direction" in p]
    non_trade = [p for p in patterns if "direction" not in p]
    
    if trade_patterns:
        lines.append("Tradeable Patterns:")
        for p in trade_patterns:
            conf = p.get("confidence", 0)
            bar = "▓" * (conf // 10) + "░" * (10 - conf // 10)
            direction = p.get("direction", "?").upper()
            pattern_type = p.get("pattern", "?")
            target = p.get("target")
            inval = p.get("invalidation")
            
            lines.append(f"  [{bar}] {conf}% — {pattern_type} ({direction})")
            lines.append(f"       {p.get('description', '')}")
            if target:
                lines.append(f"       Target      : {_fmt(target)}")
            if inval:
                lines.append(f"       Invalidation: {_fmt(inval)}")
            lines.append("")
    
    # Non-trade patterns (trendlines)
    if non_trade:
        lines.append("Structure:") if trade_patterns else lines.append("Structure Patterns:")
        for p in non_trade:
            strength = p.get("strength", "")
            desc = p.get("description", "")
            r2 = p.get("r_squared", "")
            lines.append(f"  [{strength}] {desc} (R²={r2})")
        lines.append("")
    
    return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────────────────


def list_patterns():
    """List all detectable chart patterns."""
    print("Detectable Chart Patterns:")
    print()
    print("  Reversal Patterns:")
    print("    - Head and Shoulders (top / inverse)")
    print("    - Double Top / Double Bottom")
    print("    - Rising Wedge (bearish reversal)")
    print("    - Falling Wedge (bullish reversal)")
    print()
    print("  Continuation Patterns:")
    print("    - Bull Flag / Bear Flag")
    print("    - Bull Pennant / Bear Pennant")
    print()
    print("  Trend Structure:")
    print("    - Ascending Trendline (rising lows)")
    print("    - Descending Trendline (falling highs)")
    print("    - Horizontal Support / Resistance")
    print()
    print("  Timeframes: " + ", ".join(sorted(TIMEFRAME_MAP.keys())))


def main():
    parser = argparse.ArgumentParser(
        description="Kairos — Chart Pattern Detection",
    )
    parser.add_argument("--coin", "-c", default="bitcoin",
                        help="Coin name or ID (default: bitcoin).")
    parser.add_argument("--timeframe", "-t", default="1h",
                        choices=list(TIMEFRAME_MAP.keys()),
                        help="Candle timeframe (default: 1h).")
    parser.add_argument("--json", "-j", action="store_true",
                        help="Output as JSON.")
    parser.add_argument("--list-patterns", action="store_true",
                        help="List detectable chart patterns.")
    args = parser.parse_args()

    if args.list_patterns:
        list_patterns()
        return

    report = build_pattern_report(coin=args.coin, timeframe=args.timeframe, json_mode=args.json)
    print(report)


if __name__ == "__main__":
    main()
