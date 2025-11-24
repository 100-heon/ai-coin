from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

from . import data_access

app = FastAPI(title="AI-Trader Dashboard API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static dashboard (frontend)
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/web", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


@app.get("/")
def serve_root():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return {"message": "Dashboard UI not found. Visit /web"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/summary")
def api_summary():
    return data_access.summary()


@app.get("/api/signatures")
def api_signatures():
    return {"signatures": data_access.list_signatures()}


@app.get("/api/positions/{signature}")
def api_positions(signature: str, limit: Optional[int] = Query(default=100, ge=1, le=5000)):
    rows = data_access.get_positions(signature, limit=limit)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No position data for signature '{signature}'")
    return {"signature": signature, "count": len(rows), "records": rows}


@app.get("/api/positions/{signature}/latest")
def api_latest_position(signature: str):
    row = data_access.latest_position(signature)
    if not row:
        raise HTTPException(status_code=404, detail=f"No position data for signature '{signature}'")
    return row


@app.get("/api/metrics/{signature}")
def api_metrics(signature: str, limit: Optional[int] = Query(default=50, ge=1, le=2000)):
    rows = data_access.get_metrics(signature, limit=limit)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No metrics data for signature '{signature}'")
    return {"signature": signature, "count": len(rows), "records": rows}


@app.get("/api/metrics/{signature}/latest")
def api_latest_metrics(signature: str):
    row = data_access.latest_metrics(signature)
    if not row:
        raise HTTPException(status_code=404, detail=f"No metrics data for signature '{signature}'")
    return row


@app.get("/api/portfolio/{signature}")
def api_portfolio_timeseries(signature: str, limit: Optional[int] = Query(default=None, ge=1, le=5000)):
    rows = data_access.portfolio_timeseries(signature, limit=limit)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No portfolio data for signature '{signature}'")
    return {"signature": signature, "count": len(rows), "records": rows}


@app.get("/api/logs/{signature}")
def api_log_dates(signature: str):
    dates = data_access.list_log_dates(signature)
    if not dates:
        raise HTTPException(status_code=404, detail=f"No log data for signature '{signature}'")
    return {"signature": signature, "dates": dates}


@app.get("/api/logs/{signature}/{date}")
def api_logs(signature: str, date: str, limit: Optional[int] = Query(default=None, ge=1, le=5000)):
    rows = data_access.get_log_records(signature, date, limit=limit)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No log records for signature '{signature}' on {date}")
    return {"signature": signature, "date": date, "count": len(rows), "records": rows}


@app.get("/api/holdings/{signature}")
def api_holdings(signature: str):
    data = data_access.holdings_with_prices(signature)
    return data
