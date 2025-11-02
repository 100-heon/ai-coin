import json
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_DATA_DIR = REPO_ROOT / "data" / "agent_data"


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
    rows = _jsonl_rows(path)
    if limit is not None and limit > 0:
        return rows[-limit:]
    return rows


def latest_position(signature: str) -> Optional[Dict[str, Any]]:
    rows = get_positions(signature, limit=1)
    return rows[-1] if rows else None


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
