import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_DATA_DIR = REPO_ROOT / "data" / "agent_data"
WATCHLIST_FILE = REPO_ROOT / "data" / "watchlist.json"
UPBIT_API_BASE = os.environ.get("UPBIT_API_BASE", "https://api.upbit.com")
QUOTE_CCY = os.environ.get("UPBIT_QUOTE", "KRW").upper()
CACHE_TTL = float(os.environ.get("API_CACHE_TTL", "5"))
_CACHE: Dict[str, tuple[float, Any]] = {}


def _cached(key: str, loader, ttl: Optional[float] = None):
    ttl = ttl if ttl is not None else CACHE_TTL
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = loader()
    _CACHE[key] = (now, value)
    return value


def list_signatures() -> List[str]:
    if not AGENT_DATA_DIR.exists():
        return []
    return sorted(
        entry.name
        for entry in AGENT_DATA_DIR.iterdir()
        if entry.is_dir()
    )


def _jsonl_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def get_positions(signature: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    path = AGENT_DATA_DIR / signature / "position" / "position.jsonl"
    rows = _cached(f"positions:{signature}", lambda: _jsonl_rows(path))
    if limit is not None and limit > 0:
        return rows[-limit:].copy()
    return rows.copy()


def _load_watchlist_symbols() -> List[str]:
    watch = os.environ.get("WATCHLIST_SYMBOLS")
    if watch:
        return [sym.strip() for sym in watch.split(",") if sym.strip()]
    if WATCHLIST_FILE.exists():
        try:
            data = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
            symbols = data.get("symbols")
            if isinstance(symbols, list):
                return [str(sym) for sym in symbols]
        except Exception:
            return []
    return []


def latest_position(signature: str) -> Optional[Dict[str, Any]]:
    rows = get_positions(signature, limit=1)
    latest = rows[-1] if rows else None
    if latest:
        watchlist = _load_watchlist_symbols()
        if watchlist:
            latest["watchlist"] = watchlist
    return latest


def get_metrics(signature: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    path = AGENT_DATA_DIR / signature / "metrics" / "metrics.jsonl"
    rows = _jsonl_rows(path)
    if limit is not None and limit > 0:
        return rows[-limit:]
    return rows


def latest_metrics(signature: str) -> Optional[Dict[str, Any]]:
    rows = get_metrics(signature, limit=1)
    return rows[-1] if rows else None


def list_log_dates(signature: str) -> List[str]:
    log_root = AGENT_DATA_DIR / signature / "log"
    if not log_root.exists():
        return []
    dates = [
        entry.name
        for entry in log_root.iterdir()
        if entry.is_dir()
    ]
    return sorted(dates)


def get_log_records(signature: str, date: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    path = AGENT_DATA_DIR / signature / "log" / date / "log.jsonl"
    rows = _jsonl_rows(path)
    if limit is not None and limit > 0:
        return rows[-limit:]
    return rows


def summary() -> Dict[str, Any]:
    signatures = list_signatures()
    overview: List[Dict[str, Any]] = []
    for sig in signatures:
        latest = latest_position(sig)
        cash = None
        if latest:
            cash_value = latest.get("positions", {}).get("CASH")
            if isinstance(cash_value, (int, float)):
                cash = float(cash_value)
        overview.append({"signature": sig, "latest_cash": cash})
    return {"signatures": signatures, "overview": overview}


def _normalize_market(symbol: str) -> str:
    s = symbol.strip().upper()
    if "-" in s:
        return s
    return f"{QUOTE_CCY}-{s}"


_price_cache: Dict[tuple[str, str], float] = {}


def _get_daily_close(symbol: str, date: str) -> float:
    """Fetch Upbit daily candle close for date (uses public API; cached per symbol/date)."""
    key = (symbol, date)
    if key in _price_cache:
        return _price_cache[key]
    market = _normalize_market(symbol)
    url = f"{UPBIT_API_BASE}/v1/candles/days"
    params = {"market": market, "to": f"{date} 23:59:59", "count": 1}
    try:
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            arr = resp.json()
            if isinstance(arr, list) and arr:
                close_px = float(arr[0].get("trade_price") or 0.0)
                _price_cache[key] = close_px
                return close_px
    except Exception:
        pass
    _price_cache[key] = 0.0
    return 0.0


def portfolio_timeseries(signature: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return equity timeseries using paper-trade position snapshots and Upbit public prices."""

    def _loader():
        rows = get_positions(signature, limit=limit)
        series: List[Dict[str, Any]] = []
        for row in rows:
            date = row.get("date")
            positions = row.get("positions", {}) or {}
            cash = float(positions.get("CASH", 0.0) or 0.0)
            equity = cash
            for sym, qty in positions.items():
                if sym == "CASH":
                    continue
                try:
                    qty_val = float(qty or 0.0)
                except Exception:
                    qty_val = 0.0
                if qty_val <= 0:
                    continue
                px = _get_daily_close(sym, date)
                equity += qty_val * px
        series.append({
            "date": date,
            "timestamp": row.get("timestamp"),
            "equity": equity,
            "cash": cash,
            "realized_pnl": float(row.get("realized_pnl", 0.0) or 0.0),
        })
        return series

    cache_key = f"portfolio:{signature}:{limit}"
    return _cached(cache_key, _loader)


def _ticker_batch(symbols: List[str]) -> Dict[str, float]:
    """Fetch latest trade_price per symbol (KRW market)."""
    prices: Dict[str, float] = {}
    if not symbols:
        return prices
    markets = [_normalize_market(sym) for sym in symbols]
    url = f"{UPBIT_API_BASE}/v1/ticker"
    # batch up to 50
    for i in range(0, len(markets), 50):
        batch = markets[i:i+50]
        try:
            resp = requests.get(url, params={"markets": ",".join(batch)}, timeout=10)
            if resp.status_code != 200:
                continue
            data = resp.json()
            if isinstance(data, list):
                for item in data:
                    m = item.get("market", "")
                    sym = m.split("-", 1)[1] if "-" in m else m
                    try:
                        prices[sym] = float(item.get("trade_price") or 0.0)
                    except Exception:
                        prices[sym] = 0.0
        except Exception:
            continue
    return prices


def holdings_with_prices(signature: str) -> Dict[str, Any]:
    """Return latest holdings with live prices and valuation."""

    def _loader():
        latest = latest_position(signature)
        if not latest:
            return {"holdings": [], "positions": {}, "avg_costs": {}, "realized_pnl": 0.0}
        positions = latest.get("positions", {}) or {}
        avg_costs = latest.get("avg_costs", {}) or {}
        realized_pnl = float(latest.get("realized_pnl", 0.0) or 0.0)
        coins = [sym for sym, qty in positions.items() if sym != "CASH" and (qty or 0) > 0]
        tickers = _ticker_batch(coins)

        holdings = []
        for sym in coins:
            qty = float(positions.get(sym, 0.0) or 0.0)
            last_px = float(tickers.get(sym, 0.0) or 0.0)
            avg = float(avg_costs.get(sym, 0.0) or 0.0)
            value = qty * last_px
            holdings.append({
                "symbol": sym,
                "quantity": qty,
                "avg_cost": avg,
                "last_price": last_px,
                "value": value,
            })

        return {
            "holdings": holdings,
            "positions": positions,
            "avg_costs": avg_costs,
            "realized_pnl": realized_pnl,
            "initial_cash": _get_initial_cash(signature),
        }

    return _cached(f"holdings:{signature}", _loader, ttl=3.0)


def _get_initial_cash(signature: str) -> float:
    try:
        return float(os.environ.get("START_CASH_KRW", "100000000") or 100000000)
    except Exception:
        return 100000000.0


def get_trade_actions(signature: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return buy/sell actions with timestamp and fill data."""
    rows = get_positions(signature)
    actions: List[Dict[str, Any]] = []
    for row in rows:
        action = row.get("this_action") or {}
        act_type = action.get("action")
        if act_type not in ("buy", "sell"):
            continue
        symbol = action.get("symbol", "")
        amount = action.get("amount")
        fill_price = action.get("fill_price") or action.get("price")
        timestamp = row.get("timestamp")

        krw_delta: Optional[float] = None
        if act_type == "buy":
            val = action.get("krw_spent") or action.get("requested_krw")
            if val is not None:
                krw_delta = -abs(float(val))
        else:  # sell
            val = action.get("proceeds_krw") or action.get("price")
            if val is not None:
                krw_delta = abs(float(val))

        actions.append({
            "timestamp": timestamp,
            "date": row.get("date"),
            "id": row.get("id"),
            "action": act_type,
            "symbol": symbol,
            "amount": amount,
            "fill_price": fill_price,
            "krw_delta": krw_delta,
        })

    if limit and limit > 0:
        return actions[-limit:]
    return actions
