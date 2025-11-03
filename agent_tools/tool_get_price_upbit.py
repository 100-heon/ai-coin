from fastmcp import FastMCP
from pathlib import Path
from typing import Dict, Any, List, Optional
import os
from dotenv import load_dotenv
load_dotenv()

import requests
from datetime import datetime

mcp = FastMCP("LocalPrices")

UPBIT_API_BASE = os.environ.get("UPBIT_API_BASE", "https://api.upbit.com")
QUOTE_CCY = os.environ.get("UPBIT_QUOTE", "KRW").upper()


def _normalize_market(symbol: str) -> str:
    s = symbol.strip().upper()
    if "-" in s:
        return s
    return f"{QUOTE_CCY}-{s}"


def _bar_minutes_from_env(default: int = 10) -> int:
    """Resolve minute bar from env.

    Supports either:
      - UPBIT_BAR="10m", "60m", "4h", "240" ...
      - UPBIT_BAR_MINUTES="10", "60", "240"
    """
    raw = os.environ.get("UPBIT_BAR")
    if raw:
        v = raw.strip().lower()
        if v.endswith("m") and v[:-1].isdigit():
            return max(1, int(v[:-1]))
        if v.endswith("h") and v[:-1].isdigit():
            return max(1, int(v[:-1]) * 60)
        if v.isdigit():
            return max(1, int(v))
    raw2 = os.environ.get("UPBIT_BAR_MINUTES")
    if raw2 and raw2.isdigit():
        return max(1, int(raw2))
    return default


def _bar_count_from_env(default: int = 30) -> int:
    """Resolve minutes-candle history length from env.

    Uses UPBIT_BAR_COUNT when set; falls back to provided default (30).
    """
    raw = os.environ.get("UPBIT_BAR_COUNT")
    if raw and str(raw).isdigit():
        try:
            v = int(str(raw))
            return max(1, v)
        except Exception:
            pass
    return default


def _validate_date(date_str: str) -> None:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("date must be in YYYY-MM-DD format") from exc


def _get_daily_candle(symbol: str, date: str) -> Dict[str, Any] | None:
    # Upbit daily candles: use 'to' param (UTC). We set to end of day to include the date's candle.
    # Fallback to latest if the exact date is not present.
    market = _normalize_market(symbol)
    url = f"{UPBIT_API_BASE}/v1/candles/days"

    # Try requested date first
    to_ts = f"{date} 23:59:59"
    params = {"market": market, "to": to_ts, "count": 1}
    try:
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            arr = resp.json()
            if isinstance(arr, list) and arr:
                return arr[0]
    except Exception:
        pass

    # Fallback to latest
    try:
        resp = requests.get(url, params={"market": market, "count": 1}, timeout=10)
        if resp.status_code == 200:
            arr = resp.json()
            if isinstance(arr, list) and arr:
                return arr[0]
    except Exception:
        pass
    return None


def _get_minutes_candles(symbol: str, minutes: int = 10, count: int = 30, to: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetch Upbit minute candles.

    Args:
        symbol: e.g. 'BTC' or 'KRW-BTC'
        minutes: one of 1,3,5,10,15,30,60,240
        count: number of candles (max 200 per Upbit docs)
        to: optional end time 'YYYY-MM-DD HH:MM:SS' (KST supported by Upbit API)

    Returns:
        List of candle dicts (most recent first per Upbit), possibly empty on error.
    """
    # Normalize to Upbit-supported units
    unit = int(minutes or _bar_minutes_from_env(10))
    allowed = {1, 3, 5, 10, 15, 30, 60, 240}
    if unit not in allowed:
        # Round to nearest allowed unit
        unit = min(allowed, key=lambda x: abs(x - unit))
    market = _normalize_market(symbol)
    url = f"{UPBIT_API_BASE}/v1/candles/minutes/{unit}"
    params: Dict[str, Any] = {"market": market, "count": int(count)}
    if to:
        params["to"] = to
    try:
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code != 200:
            return []
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


@mcp.tool()
def get_price_local(symbol: str, date: str) -> Dict[str, Any]:
    """Return OHLCV for a symbol on (or nearest before) date using Upbit daily candles.

    Args:
        symbol: e.g. 'BTC' or 'KRW-BTC'.
        date: 'YYYY-MM-DD'.

    Returns:
        { symbol, date, ohlcv: {open, high, low, close, volume} } or error.
    """
    try:
        _validate_date(date)
    except ValueError as e:
        return {"error": str(e), "symbol": symbol, "date": date}

    candle = _get_daily_candle(symbol, date)
    if not candle:
        return {"error": "No candle data from Upbit", "symbol": symbol, "date": date}

    # Upbit fields: opening_price, high_price, low_price, trade_price(close), candle_acc_trade_volume
    return {
        "symbol": _normalize_market(symbol),
        "date": date,
        "ohlcv": {
            "open": candle.get("opening_price"),
            "high": candle.get("high_price"),
            "low": candle.get("low_price"),
            "close": candle.get("trade_price"),
            "volume": candle.get("candle_acc_trade_volume"),
        },
    }


@mcp.tool()
def get_price_minutes(symbol: str, minutes: int | None = None, count: int | None = None, to: str | None = None) -> Dict[str, Any]:
    """Return recent minute candles for a symbol.

    Args:
        symbol: 'BTC' or 'KRW-BTC'
        minutes: 1,3,5,10,15,30,60,240 (default 10)
        count: number of candles to fetch (default 30)
        to: optional end time 'YYYY-MM-DD HH:MM:SS' (KST)

    Returns:
        { symbol, minutes, count, to, candles: [ {time, open, high, low, close, volume}... ] }
    """
    # If minutes/count not provided by the caller, read from env
    minutes = int(minutes) if minutes is not None else _bar_minutes_from_env(10)
    count = int(count) if count is not None else _bar_count_from_env(30)
    candles = _get_minutes_candles(symbol, minutes=minutes, count=count, to=to)
    formatted: List[Dict[str, Any]] = []
    for c in candles:
        formatted.append({
            "time": c.get("candle_date_time_kst") or c.get("candle_date_time_utc"),
            "open": c.get("opening_price"),
            "high": c.get("high_price"),
            "low": c.get("low_price"),
            "close": c.get("trade_price"),
            "volume": c.get("candle_acc_trade_volume"),
        })
    return {
        "symbol": _normalize_market(symbol),
        "minutes": int(minutes),
        "count": int(count),
        "to": to,
        "candles": formatted,
    }


@mcp.tool()
def get_ticker_batch(symbols: List[str] | str) -> Dict[str, Any]:
    """Fetch current ticker data for a list of KRW symbols in one call.

    Args:
        symbols: List of symbols like ["BTC","ETH","SOL"] or a comma-separated string.

    Returns:
        { "results": [ {symbol, market, trade_price, signed_change_rate, acc_trade_price_24h, status} ... ] }
        If a symbol is not available in KRW market or request fails, status explains the reason.
    """
    # Normalize input to list[str]
    if isinstance(symbols, str):
        raw_list = [s.strip() for s in symbols.split(",") if s.strip()]
    else:
        raw_list = list(symbols or [])

    markets: List[str] = []
    for s in raw_list:
        if not s:
            continue
        ms = s.strip().upper()
        markets.append(ms if "-" in ms else f"{QUOTE_CCY}-{ms}")

    results: List[Dict[str, Any]] = []
    if not markets:
        return {"results": results}

    url = f"{UPBIT_API_BASE}/v1/ticker"
    # batch up to 50 per request
    for i in range(0, len(markets), 50):
        batch = markets[i:i+50]
        try:
            resp = requests.get(url, params={"markets": ",".join(batch)}, timeout=10)
            if resp.status_code != 200:
                # mark all as error
                for m in batch:
                    sym = m.split("-", 1)[1] if "-" in m else m
                    results.append({"symbol": sym, "market": m, "status": f"http_{resp.status_code}"})
                continue
            data = resp.json()
            if isinstance(data, list):
                for item in data:
                    m = item.get("market", "")
                    sym = m.split("-", 1)[1] if "-" in m else m
                    results.append({
                        "symbol": sym,
                        "market": m,
                        "trade_price": item.get("trade_price"),
                        "signed_change_rate": item.get("signed_change_rate"),
                        "acc_trade_price_24h": item.get("acc_trade_price_24h"),
                        "status": "ok",
                    })
        except Exception:
            for m in batch:
                sym = m.split("-", 1)[1] if "-" in m else m
                results.append({"symbol": sym, "market": m, "status": "error"})

    return {"results": results}


if __name__ == "__main__":
    port = int(os.getenv("GETPRICE_HTTP_PORT", "8003"))
    mcp.run(transport="streamable-http", port=port)
