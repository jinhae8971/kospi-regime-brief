#!/usr/bin/env python3
"""EWY/KOSPI correction monitor.

The script collects market and investor-flow data, publishes an explainable
rules-based snapshot for GitHub Pages, appends a compact history record and can
send a short Telegram brief.  It deliberately separates US and Korean market
dates because EWY also embeds USD/KRW moves and can trade while Korea is closed.
"""

from __future__ import annotations

import argparse
import html
import io
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LATEST_PATH = DATA_DIR / "latest.json"
HISTORY_PATH = DATA_DIR / "history.json"
METHODOLOGY_PATH = DATA_DIR / "methodology.json"
MACRO_CACHE_PATH = DATA_DIR / "macro_cache.json"
MACRO_CACHE_MAX = 800
NOTIFICATION_PATH = DATA_DIR / "notification.json"
KST = timezone(timedelta(hours=9))

SCHEMA_VERSION = 1
METHODOLOGY_VERSION = "2026-07-18.v1"
PAGE_URL = "https://jinhae8971.github.io/kospi-regime-brief/"
PAGE_DATA_URL = PAGE_URL + "data/latest.json"

LEVELS = {
    "deep_support": 135.48,
    "abc_equal": 145.81,
    "invalidation": 153.00,
    "primary_support": 155.58,
    "recovery_low": 169.00,
    "recovery_high": 172.00,
    "trend_confirm": 177.50,
    "strong_confirm": 184.20,
    "wave_b": 192.25,
}

WAVE = {
    "high": 220.09,
    "a_low": 174.45,
    "b_high": 192.25,
    "fib_support": 155.58,
}

# The initial 2026-07-17 baseline is transcribed from the chart supplied with
# this analysis. Nasdaq historical data can lag a session on weekends; the
# bootstrap is used only while that date is absent and is superseded
# automatically by the provider's official observation or any newer session.
EWY_BOOTSTRAP = {
    "date": "2026-07-17",
    "open": 156.61,
    "high": 169.00,
    "low": 154.20,
    "close": 162.54,
    "volume": 37_988_363,
    "source": "user_supplied_chart_initial_baseline",
}


class DataError(RuntimeError):
    """Raised when a required market dataset cannot be collected safely."""


@dataclass
class Factor:
    key: str
    label: str
    points: int
    max_points: int
    available: bool
    reason: str
    coverage_points: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "points": self.points,
            "max_points": self.max_points,
            "available": self.available,
            "coverage_points": self.coverage_points if self.coverage_points is not None else (self.max_points if self.available else 0),
            "reason": self.reason,
        }


def now_kst() -> datetime:
    return datetime.now(KST)


def clamp(low: float, high: float, value: float) -> float:
    return max(low, min(high, value))


def safe_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    text = str(value).strip().replace(",", "").replace("$", "").replace("%", "")
    if text in {"", "-", "--", "N/A", "null", "None"}:
        return None
    text = re.sub(r"[^0-9+\-.]", "", text)
    try:
        return float(text)
    except ValueError:
        return None


def json_dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def http_session() -> requests.Session:
    retry = Retry(
        total=3,
        read=3,
        connect=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "Accept": "application/json,text/plain,*/*",
        }
    )
    return session


def nasdaq_headers() -> dict[str, str]:
    return {
        "Origin": "https://www.nasdaq.com",
        "Referer": "https://www.nasdaq.com/",
    }


def fetch_nasdaq_history(
    session: requests.Session, symbol: str, assetclass: str = "etf", days: int = 760
) -> tuple[pd.DataFrame, dict[str, Any]]:
    end = now_kst().date()
    start = end - timedelta(days=days)
    url = f"https://api.nasdaq.com/api/quote/{symbol}/historical"
    response = session.get(
        url,
        params={
            "assetclass": assetclass,
            "fromdate": start.isoformat(),
            "todate": end.isoformat(),
            "limit": 5000,
        },
        headers=nasdaq_headers(),
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") or {}
    rows = ((data.get("tradesTable") or {}).get("rows") or [])
    parsed: list[dict[str, Any]] = []
    for row in rows:
        try:
            dt = datetime.strptime(row["date"], "%m/%d/%Y").date()
        except (KeyError, TypeError, ValueError):
            continue
        close = safe_float(row.get("close"))
        if close is None:
            continue
        parsed.append(
            {
                "date": pd.Timestamp(dt),
                "open": safe_float(row.get("open")),
                "high": safe_float(row.get("high")),
                "low": safe_float(row.get("low")),
                "close": close,
                "volume": safe_float(row.get("volume")),
            }
        )
    if len(parsed) < 60:
        raise DataError(f"Nasdaq {symbol}: usable history is too short ({len(parsed)})")
    frame = pd.DataFrame(parsed).set_index("date").sort_index()
    frame = frame[~frame.index.duplicated(keep="last")]
    meta = {
        "name": f"Nasdaq {symbol} historical",
        "url": f"https://www.nasdaq.com/market-activity/etf/{symbol.lower()}/historical",
        "as_of": frame.index[-1].date().isoformat(),
        "status": "ok",
    }
    return frame, meta


def fetch_nasdaq_current(
    session: requests.Session, symbol: str, assetclass: str = "etf"
) -> dict[str, Any] | None:
    base = f"https://api.nasdaq.com/api/quote/{symbol}"
    try:
        info = session.get(
            base + "/info",
            params={"assetclass": assetclass},
            headers=nasdaq_headers(),
            timeout=20,
        )
        info.raise_for_status()
        info_data = (info.json().get("data") or {})
        # Only the provider's explicit regular-session close is eligible.
        # Primary data can be pre-market, intraday or after-hours.
        official = info_data.get("secondaryData") or {}
        timestamp = str(official.get("lastTradeTimestamp") or "")
        if "Closed at" not in timestamp:
            return None
        match = re.search(r"([A-Z][a-z]{2} \d{1,2}, \d{4})", timestamp)
        if not match:
            return None
        observed_date = datetime.strptime(match.group(1), "%b %d, %Y").date()
        close = safe_float(official.get("lastSalePrice"))
        if close is None:
            return None

        high = low = volume = None
        trade = session.get(
            base + "/realtime-trades",
            params={"limit": 1, "fromTime": "00:00"},
            headers=nasdaq_headers(),
            timeout=20,
        )
        trade.raise_for_status()
        rows = ((((trade.json().get("data") or {}).get("topTable") or {}).get("rows")) or [])
        if rows:
            high_low = str(rows[0].get("todayHighLow") or "")
            parts = high_low.split("/")
            if len(parts) == 2:
                high, low = safe_float(parts[0]), safe_float(parts[1])
            volume = safe_float(rows[0].get("nlsVolume"))
        return {
            "date": observed_date.isoformat(),
            "open": None,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "change_pct": safe_float(official.get("percentageChange")),
            "source": "nasdaq_realtime",
        }
    except Exception as exc:  # optional freshness enhancement
        print(f"[warn] Nasdaq current {symbol}: {exc}", file=sys.stderr)
        return None


def merge_current_observation(
    frame: pd.DataFrame,
    current: dict[str, Any] | None,
    bootstrap: dict[str, Any] | None = None,
) -> pd.DataFrame:
    out = frame.copy()
    last_date = out.index[-1].date()
    candidate = current
    if bootstrap:
        bootstrap_date = date.fromisoformat(bootstrap["date"])
        if bootstrap_date > last_date and (
            candidate is None or date.fromisoformat(candidate["date"]) <= bootstrap_date
        ):
            candidate = bootstrap
    if not candidate:
        return out
    observed = date.fromisoformat(candidate["date"])
    if observed <= last_date:
        return out
    previous_close = float(out["close"].iloc[-1])
    row = {
        "open": safe_float(candidate.get("open")) or previous_close,
        "high": safe_float(candidate.get("high")) or safe_float(candidate.get("close")),
        "low": safe_float(candidate.get("low")) or safe_float(candidate.get("close")),
        "close": safe_float(candidate.get("close")),
        "volume": safe_float(candidate.get("volume")),
    }
    out.loc[pd.Timestamp(observed), list(row)] = list(row.values())
    return out.sort_index()


def naver_get(session: requests.Session, path: str, **params: Any) -> Any:
    response = session.get(
        "https://m.stock.naver.com" + path,
        params=params,
        headers={"Referer": "https://m.stock.naver.com/"},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def parse_naver_prices(rows: Iterable[dict[str, Any]]) -> pd.DataFrame:
    parsed: list[dict[str, Any]] = []
    for row in rows:
        try:
            dt = pd.Timestamp(row["localTradedAt"])
        except (KeyError, TypeError, ValueError):
            continue
        close = safe_float(row.get("closePrice"))
        if close is None:
            continue
        parsed.append(
            {
                "date": dt,
                "open": safe_float(row.get("openPrice")),
                "high": safe_float(row.get("highPrice")),
                "low": safe_float(row.get("lowPrice")),
                "close": close,
                "volume": safe_float(row.get("accumulatedTradingVolume")),
            }
        )
    if not parsed:
        raise DataError("Naver returned no usable price observations")
    return pd.DataFrame(parsed).set_index("date").sort_index()


def fetch_naver_index(session: requests.Session, page_size: int = 60, pages: int = 4) -> tuple[pd.DataFrame, dict[str, Any]]:
    # Naver currently rejects pageSize > 60, so collect a longer window page by page.
    rows: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        rows.extend(naver_get(session, "/api/index/KOSPI/price", pageSize=page_size, page=page))
    frame = parse_naver_prices(rows)
    return frame, {
        "name": "Naver KOSPI daily prices",
        "url": "https://m.stock.naver.com/domestic/index/KOSPI/total",
        "as_of": frame.index[-1].date().isoformat(),
        "status": "ok",
    }


def fetch_naver_stock(session: requests.Session, code: str, page_size: int = 60) -> pd.DataFrame:
    rows = naver_get(session, f"/api/stock/{code}/price", pageSize=page_size, page=1)
    return parse_naver_prices(rows)


def fetch_naver_fx(session: requests.Session, page_size: int = 60) -> tuple[pd.Series, dict[str, Any]]:
    response = session.get(
        "https://api.stock.naver.com/marketindex/exchange/FX_USDKRW/prices",
        params={"page": 1, "pageSize": page_size},
        headers={"Referer": "https://finance.naver.com/"},
        timeout=20,
    )
    response.raise_for_status()
    observations: dict[pd.Timestamp, float] = {}
    for row in response.json():
        value = safe_float(row.get("closePrice"))
        try:
            observed = pd.Timestamp(row["localTradedAt"])
        except (KeyError, TypeError, ValueError):
            continue
        if value is not None:
            observations[observed] = value
    series = pd.Series(observations, dtype="float64").sort_index()
    if len(series) < 20:
        raise DataError(f"Naver USD/KRW history is too short ({len(series)})")
    return series, {
        "name": "Naver USD/KRW reference rate",
        "url": "https://finance.naver.com/marketindex/",
        "as_of": series.index[-1].date().isoformat(),
        "status": "ok",
    }


def fetch_naver_flows(
    session: requests.Session, trading_dates: Iterable[pd.Timestamp], limit: int = 20
) -> tuple[pd.DataFrame, dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    dates = list(trading_dates)[-limit:]
    for dt in dates:
        try:
            row = naver_get(session, "/api/index/KOSPI/trend", bizdate=dt.strftime("%Y%m%d"))
            # Naver values are KRW 100 million. Convert to KRW trillion.
            values = {
                "foreign": safe_float(row.get("foreignValue")),
                "institution": safe_float(row.get("institutionalValue")),
                "personal": safe_float(row.get("personalValue")),
            }
            if any(value is None for value in values.values()):
                raise DataError("required investor-flow field is missing")
            parsed.append(
                {
                    "date": pd.Timestamp(datetime.strptime(row["bizdate"], "%Y%m%d").date()),
                    "foreign": values["foreign"] / 10_000.0,
                    "institution": values["institution"] / 10_000.0,
                    "personal": values["personal"] / 10_000.0,
                }
            )
        except Exception as exc:
            print(f"[warn] Naver flow {dt.date()}: {exc}", file=sys.stderr)
    if len(parsed) < 10:
        raise DataError(f"Naver investor-flow history is too short ({len(parsed)})")
    frame = pd.DataFrame(parsed).drop_duplicates("date", keep="last").set_index("date").sort_index()
    expected = {pd.Timestamp(item).normalize() for item in dates[-10:]}
    actual = {pd.Timestamp(item).normalize() for item in frame.index}
    if not expected.issubset(actual):
        missing = sorted(item.date().isoformat() for item in expected - actual)
        raise DataError(f"Naver investor-flow dates are incomplete: {', '.join(missing)}")
    return frame, {
        "name": "Naver/KRX investor trend",
        "url": "https://m.stock.naver.com/domestic/index/KOSPI/total",
        "as_of": frame.index[-1].date().isoformat(),
        "status": "ok",
        "unit": "KRW trillion",
    }


FRED_TIMEOUT = 8

# FRED graph 엔드포인트가 러너 IP에서 차단될 때 쓰는 대체 심볼.
# scale: FRED 시계열과 단위를 맞추기 위한 배수.
# approx=True 는 바스켓이 달라 근사치임을 뜻한다(대시보드에 명시됨).
FRED_PROXY = {
    "VIXCLS": {"symbol": "^VIX", "scale": 1.0, "approx": False, "note": "CBOE VIX 종가"},
    "DGS10": {"symbol": "^TNX", "scale": 1.0, "approx": False, "note": "10년물 금리 지수"},
    "DTWEXBGS": {"symbol": "DX-Y.NYB", "scale": 1.0, "approx": True,
                 "note": "DXY — 광의 무역가중 달러와 바스켓 구성이 다름"},
    "DEXKOUS": {"symbol": "KRW=X", "scale": 1.0, "approx": False, "note": "USD/KRW 현물"},
}


def _trim_series(frame: pd.DataFrame, series_id: str, days: int) -> pd.Series:
    cutoff = pd.Timestamp(now_kst().date() - timedelta(days=days))
    frame = frame.dropna().loc[lambda x: x["date"] >= cutoff].set_index("date")
    if len(frame) < 5:
        raise DataError(f"FRED {series_id}: usable history is too short")
    return frame["value"].sort_index()


def fetch_fred_api(session: requests.Session, series_id: str, days: int = 760):
    """1순위 — FRED 공식 API. FRED_API_KEY 가 있을 때만 시도한다."""
    api_key = os.environ.get("FRED_API_KEY", "").strip()
    if not api_key:
        raise DataError("FRED_API_KEY is not set")
    start = (now_kst().date() - timedelta(days=days)).isoformat()
    response = session.get(
        "https://api.stlouisfed.org/fred/series/observations",
        params={"series_id": series_id, "api_key": api_key, "file_type": "json",
                "observation_start": start},
        timeout=FRED_TIMEOUT,
    )
    response.raise_for_status()
    rows = response.json().get("observations") or []
    frame = pd.DataFrame([{"date": r.get("date"), "value": r.get("value")} for r in rows])
    if frame.empty:
        raise DataError(f"FRED {series_id}: API returned no observations")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    series = _trim_series(frame, series_id, days)
    return series, {
        "name": f"FRED {series_id} (API)",
        "url": f"https://fred.stlouisfed.org/series/{series_id}",
        "as_of": series.index[-1].date().isoformat(),
        "status": "ok",
    }


def fetch_fred_proxy(series_id: str, days: int = 760):
    """3순위 — FRED 도달 실패 시 시장 데이터로 대체한다."""
    proxy = FRED_PROXY.get(series_id)
    if not proxy:
        raise DataError(f"FRED {series_id}: no proxy defined")
    import yfinance as yf

    history = yf.Ticker(proxy["symbol"]).history(period="2y")
    if history.empty or "Close" not in history:
        raise DataError(f"{proxy['symbol']}: empty history")
    closes = history["Close"].dropna() * proxy["scale"]
    if closes.empty:
        raise DataError(f"{proxy['symbol']}: no usable closes")
    frame = pd.DataFrame({
        "date": pd.to_datetime(closes.index.date),
        "value": pd.to_numeric(closes.values, errors="coerce"),
    })
    series = _trim_series(frame, series_id, days)
    return series, {
        "name": f"{proxy['symbol']} (FRED {series_id} 대체)",
        "url": f"https://finance.yahoo.com/quote/{proxy['symbol']}",
        "as_of": series.index[-1].date().isoformat(),
        "status": "proxy_approx" if proxy["approx"] else "proxy",
        "note": proxy["note"],
    }


def fetch_fred(session: requests.Session, series_id: str, days: int = 760) -> tuple[pd.Series, dict[str, Any]]:
    response = session.get(
        "https://fred.stlouisfed.org/graph/fredgraph.csv",
        params={"id": series_id},
        timeout=FRED_TIMEOUT,
    )
    response.raise_for_status()
    frame = pd.read_csv(io.StringIO(response.text))
    frame.columns = ["date", "value"]
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    cutoff = pd.Timestamp(now_kst().date() - timedelta(days=days))
    frame = frame.dropna().loc[lambda x: x["date"] >= cutoff].set_index("date")
    if len(frame) < 5:
        raise DataError(f"FRED {series_id}: usable history is too short")
    series = frame["value"].sort_index()
    return series, {
        "name": f"FRED {series_id}",
        "url": f"https://fred.stlouisfed.org/series/{series_id}",
        "as_of": series.index[-1].date().isoformat(),
        "status": "ok",
    }



# --- 무키 공식 소스 (FRED 차단 시 1차 대안) -------------------------------

def fetch_treasury_10y(session: requests.Session, days: int = 760):
    """美 재무부 일별 국채수익률 곡선 — DGS10 의 원출처. API 키 불필요."""
    year = now_kst().year
    rows = []
    for target in (year, year - 1):
        response = session.get(
            "https://home.treasury.gov/resource-center/data-chart-center/"
            f"interest-rates/daily-treasury-rates.csv/{target}/all",
            params={"type": "daily_treasury_yield_curve",
                    "field_tdr_date_value": target, "_format": "csv"},
            timeout=25,
        )
        response.raise_for_status()
        part = pd.read_csv(io.StringIO(response.text))
        if "10 Yr" in part.columns:
            rows.append(part[["Date", "10 Yr"]].rename(columns={"Date": "date", "10 Yr": "value"}))
    if not rows:
        raise DataError("Treasury: 10Y column not found")
    frame = pd.concat(rows, ignore_index=True)
    frame["date"] = pd.to_datetime(frame["date"], format="%m/%d/%Y", errors="coerce")
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    series = _trim_series(frame.drop_duplicates("date"), "DGS10", days)
    return series, {
        "name": "US Treasury 일별 수익률곡선 10Y",
        "url": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates",
        "as_of": series.index[-1].date().isoformat(),
        "status": "ok",
    }


def fetch_cboe_vix(session: requests.Session, days: int = 760):
    """CBOE 공식 VIX 일별 종가 — VIXCLS 의 원출처. API 키 불필요."""
    response = session.get(
        "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
        timeout=25,
    )
    response.raise_for_status()
    frame = pd.read_csv(io.StringIO(response.text))[["DATE", "CLOSE"]]
    frame.columns = ["date", "value"]
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    series = _trim_series(frame, "VIXCLS", days)
    return series, {
        "name": "CBOE VIX 공식 일별 종가",
        "url": "https://www.cboe.com/tradable_products/vix/",
        "as_of": series.index[-1].date().isoformat(),
        "status": "ok",
    }


OFFICIAL_SOURCES = {"DGS10": fetch_treasury_10y, "VIXCLS": fetch_cboe_vix}


# --- 매크로 캐시 -----------------------------------------------------------

def load_macro_cache() -> dict[str, Any]:
    return load_json(MACRO_CACHE_PATH, {}) or {}


def cache_store(cache: dict, series_id: str, series: pd.Series) -> None:
    """라이브 수집분을 캐시에 병합한다. 실측값이 항상 우선한다."""
    slot = cache.setdefault("series", {}).setdefault(series_id, {})
    for stamp, value in series.items():
        if pd.notna(value):
            slot[stamp.date().isoformat()] = round(float(value), 6)
    if len(slot) > MACRO_CACHE_MAX:
        for key in sorted(slot)[:-MACRO_CACHE_MAX]:
            slot.pop(key, None)


def fetch_from_cache(cache: dict, series_id: str, days: int = 760):
    slot = (cache.get("series") or {}).get(series_id) or {}
    if len(slot) < 5:
        raise DataError(f"cache miss for {series_id}")
    frame = pd.DataFrame({"date": pd.to_datetime(list(slot.keys())),
                          "value": pd.to_numeric(list(slot.values()))})
    series = _trim_series(frame, series_id, days)
    age = (now_kst().date() - series.index[-1].date()).days
    return series, {
        "name": f"{series_id} 캐시 (실측 보관본)",
        "url": "",
        "as_of": series.index[-1].date().isoformat(),
        "status": "cache",
        "age_days": age,
    }


def fetch_macro_series(
    session: requests.Session,
    series_id: str,
    warnings: list[str],
    cache: dict[str, Any] | None = None,
    days: int = 760,
) -> tuple[pd.Series, dict[str, Any]]:
    """공식 API → FRED graph → 원출처(무키) → 시장 프록시 → 캐시 순으로 시도한다.

    앞의 네 계층은 라이브 수집이므로 성공 시 캐시에 병합한다. FRED 가 간헐적으로
    열릴 때마다 실측값이 캐시에 쌓이므로, 전 소스가 막혀도 최근 실측으로 버틴다.
    """
    cache = cache if cache is not None else {}
    attempts: list[str] = []

    def attempt(label: str, loader: Any):
        try:
            return loader()
        except Exception as exc:                       # noqa: BLE001
            attempts.append(f"{label}={str(exc).split('(Caused by')[0].strip()[:90]}")
            return None

    # FRED 계열은 우선순위대로. 확보되면 즉시 채택한다.
    tiers: list[tuple[str, Any]] = [
        ("api", lambda: fetch_fred_api(session, series_id, days)),
        ("fred", lambda: fetch_fred(session, series_id, days)),
    ]

    # 대체 계층은 순위를 고정하지 않는다. CBOE 공식 파일은 하루 늦게 갱신되고
    # 프록시가 더 신선한 날이 있어서, 실제로 받아본 뒤 기준일이 앞선 쪽을 쓴다.
    alternates: list[tuple[str, Any]] = []
    if series_id in OFFICIAL_SOURCES:
        alternates.append(("official", lambda: OFFICIAL_SOURCES[series_id](session, days)))
    alternates.append(("proxy", lambda: fetch_fred_proxy(series_id, days)))

    for label, loader in tiers:
        result = attempt(label, loader)
        if result is None:
            continue
        series, source = result

        source["tier"] = label
        if label != "cache":
            cache_store(cache, series_id, series)      # 라이브 실측만 캐시에 반영

        if label in ("official", "proxy", "cache"):
            detail = {
                "official": "원출처 직접 수집",
                "proxy": "시장 프록시" + (" · 근사치" if source.get("status") == "proxy_approx" else ""),
                "cache": f"캐시 재사용 · {source.get('age_days', '?')}일 전 실측",
            }[label]
            warnings.append(f"FRED {series_id}: {label} 계층 사용 ({detail}) — {source['name']}")
        return series, source

    # 대체 계층: 성공한 것들 중 기준일이 가장 앞선 소스를 채택
    harvested = []
    for label, loader in alternates:
        result = attempt(label, loader)
        if result is not None:
            harvested.append((label, result[0], result[1]))
    if harvested:
        harvested.sort(key=lambda item: item[2]["as_of"], reverse=True)
        label, series, source = harvested[0]
        source["tier"] = label
        cache_store(cache, series_id, series)
        for other_label, other_series, _other in harvested[1:]:
            cache_store(cache, series_id, other_series)   # 탈락분도 캐시에는 축적
        detail = ("원출처 직접 수집" if label == "official"
                  else "시장 프록시" + (" · 근사치" if source.get("status") == "proxy_approx" else ""))
        picked = f"{len(harvested)}개 후보 중 최신({source['as_of']})"
        warnings.append(f"FRED {series_id}: {label} 계층 사용 ({detail}, {picked}) — {source['name']}")
        return series, source

    # 최후: 캐시에 남은 실측값
    result = attempt("cache", lambda: fetch_from_cache(cache, series_id, days))
    if result is not None:
        series, source = result
        source["tier"] = "cache"
        warnings.append(
            f"FRED {series_id}: cache 계층 사용 (캐시 재사용 · {source.get('age_days','?')}일 전 실측)"
        )
        return series, source

    raise DataError(f"all tiers failed ({'; '.join(attempts)})")


def append_current_fx(session: requests.Session, series: pd.Series) -> tuple[pd.Series, dict[str, Any] | None]:
    try:
        response = session.get("https://open.er-api.com/v6/latest/USD", timeout=20)
        response.raise_for_status()
        payload = response.json()
        rate = safe_float((payload.get("rates") or {}).get("KRW"))
        stamp = payload.get("time_last_update_unix")
        if rate is None or stamp is None:
            return series, None
        observed = datetime.fromtimestamp(int(stamp), tz=timezone.utc).date()
        out = series.copy()
        if observed > out.index[-1].date():
            out.loc[pd.Timestamp(observed)] = rate
        return out.sort_index(), {
            "name": "Open Exchange Rates (USD/KRW)",
            "url": "https://www.exchangerate-api.com/docs/free",
            "as_of": observed.isoformat(),
            "status": "ok",
        }
    except Exception as exc:
        print(f"[warn] current USD/KRW: {exc}", file=sys.stderr)
        return series, None


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def rsi_wilder(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    result = 100 - 100 / (1 + rs)
    return result.fillna(100.0).where(avg_gain.notna())


def macd(series: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    line = ema(series, 12) - ema(series, 26)
    signal = line.ewm(span=9, adjust=False, min_periods=9).mean()
    return line, signal, line - signal


def pct_change(series: pd.Series, periods: int) -> float | None:
    clean = series.dropna()
    if len(clean) <= periods:
        return None
    return float((clean.iloc[-1] / clean.iloc[-periods - 1] - 1) * 100)


def series_metric(frame: pd.DataFrame, name: str) -> dict[str, Any]:
    close = frame["close"].dropna()
    if len(close) < 22:
        raise DataError(f"{name}: insufficient data")
    last = float(close.iloc[-1])
    previous = float(close.iloc[-2])
    result = {
        "symbol": name,
        "date": close.index[-1].date().isoformat(),
        "close": round(last, 4),
        "change_1d_pct": round((last / previous - 1) * 100, 3),
        "change_5d_pct": round(pct_change(close, 5) or 0.0, 3),
        "change_20d_pct": round(pct_change(close, 20) or 0.0, 3),
        "ma5": round(float(close.rolling(5).mean().iloc[-1]), 4),
        "ma20": round(float(close.rolling(20).mean().iloc[-1]), 4),
    }
    if "volume" in frame:
        volume = frame["volume"].dropna()
        if len(volume) >= 20:
            avg20 = float(volume.iloc[-20:].mean())
            result["volume"] = round(float(volume.iloc[-1]))
            result["volume_ratio_20d"] = round(float(volume.iloc[-1]) / avg20, 3) if avg20 else None
    return result


def weekly_metric(frame: pd.DataFrame) -> dict[str, Any]:
    weekly = frame.resample("W-FRI").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["close"])
    latest_source_day = frame.index[-1].date()
    current_week_excluded = latest_source_day.weekday() != 4
    scored = weekly.iloc[:-1] if current_week_excluded else weekly
    rsi = rsi_wilder(scored["close"])
    line, signal, hist = macd(scored["close"])
    if len(scored) < 35 or pd.isna(rsi.iloc[-1]) or pd.isna(hist.iloc[-1]):
        raise DataError("EWY weekly indicator history is insufficient")
    return {
        "date": scored.index[-1].date().isoformat(),
        "source_latest_date": latest_source_day.isoformat(),
        "provisional": False,
        "current_week_excluded": current_week_excluded,
        "rsi14": round(float(rsi.iloc[-1]), 2),
        "rsi14_prev": round(float(rsi.iloc[-2]), 2),
        "macd": round(float(line.iloc[-1]), 3),
        "macd_prev": round(float(line.iloc[-2]), 3),
        "macd_signal": round(float(signal.iloc[-1]), 3),
        "macd_signal_prev": round(float(signal.iloc[-2]), 3),
        "macd_hist": round(float(hist.iloc[-1]), 3),
        "macd_hist_prev": round(float(hist.iloc[-2]), 3),
        "macd_hist_prev2": round(float(hist.iloc[-3]), 3),
    }


def price_factor(ewy: dict[str, Any]) -> Factor:
    price = ewy["close"]
    if price >= 192.25:
        points, reason = 20, "B파 고점 192.25 상향 돌파"
    elif price >= 184.20:
        points, reason = 17, "주봉 강한 확인선 184.20 상단"
    elif price >= 177.50:
        points, reason = 12, "추세 확인선 177.50 상단"
    elif price >= 169.00:
        points, reason = 7, "1차 회복 구간 169~172 진입"
    elif price >= 155.58:
        points, reason = 3, "핵심 피보나치 지지 155.58 상단 유지"
    elif price >= 153.00:
        points, reason = 0, "153~155.58 핵심 방어 구간"
    elif price >= 145.00:
        points, reason = -10, "153 이탈, ABC 1:1 목표 접근"
    elif price >= 135.48:
        points, reason = -16, "145 이탈, 50% 되돌림 접근"
    else:
        points, reason = -20, "135.48 심화 방어선 이탈"
    return Factor("price_structure", "가격 구조", points, 20, True, reason)


def daily_factor(frame: pd.DataFrame) -> Factor:
    close = frame["close"].dropna()
    if len(close) < 30:
        return Factor("daily_reversal", "단기 반전", 0, 15, False, "데이터 부족")
    points, reasons = 0, []
    ma5 = close.rolling(5).mean()
    if close.iloc[-1] >= ma5.iloc[-1]:
        points += 3; reasons.append("5일선 상단")
    else:
        points -= 3; reasons.append("5일선 하단")
    if ma5.iloc[-1] >= ma5.iloc[-4]:
        points += 3; reasons.append("5일선 상승")
    else:
        points -= 3; reasons.append("5일선 하락")
    lows = frame["low"].dropna()
    if len(lows) >= 10 and lows.iloc[-5:].min() >= lows.iloc[-10:-5].min():
        points += 3; reasons.append("단기 저점 상승")
    else:
        points -= 3; reasons.append("단기 저점 하락")
    _, _, hist = macd(close)
    if pd.notna(hist.iloc[-1]) and pd.notna(hist.iloc[-4]) and hist.iloc[-1] >= hist.iloc[-4]:
        points += 3; reasons.append("일봉 MACD 개선")
    else:
        points -= 3; reasons.append("일봉 MACD 약화")
    volume = frame["volume"].dropna()
    if len(volume) >= 21:
        mean, std = volume.iloc[-21:-1].mean(), volume.iloc[-21:-1].std()
        z = (volume.iloc[-1] - mean) / std if std else 0
        high, low, last = frame["high"].iloc[-1], frame["low"].iloc[-1], close.iloc[-1]
        position = (last - low) / (high - low) if high > low else 0.5
        if z >= 1.5 and position >= 0.65:
            points += 3; reasons.append("대량거래 상단 마감")
        elif z >= 1.5 and position <= 0.35:
            points -= 3; reasons.append("대량거래 하단 마감")
        else:
            reasons.append("거래량 반전 확인 전")
    coverage_points = 15 if len(volume) >= 21 else 12
    return Factor(
        "daily_reversal",
        "단기 반전",
        int(clamp(-15, 15, points)),
        15,
        True,
        " · ".join(reasons),
        coverage_points,
    )


def weekly_factor(weekly: dict[str, Any]) -> Factor:
    points, reasons = 0, []
    rsi_delta = weekly["rsi14"] - weekly["rsi14_prev"]
    if rsi_delta >= 3:
        points += 4; reasons.append("주봉 RSI 반등")
    elif rsi_delta <= -3:
        points -= 4; reasons.append("주봉 RSI 하락")
    if 35 <= weekly["rsi14"] <= 55 and rsi_delta > 0:
        points += 2; reasons.append("강세장 RSI 지지")
    elif (weekly["rsi14"] >= 65 or weekly["rsi14"] < 35) and rsi_delta < 0:
        points -= 2; reasons.append("RSI 하락 압력")
    crossed_up = weekly["macd"] > weekly["macd_signal"] and weekly["macd_prev"] <= weekly["macd_signal_prev"]
    crossed_down = weekly["macd"] < weekly["macd_signal"] and weekly["macd_prev"] >= weekly["macd_signal_prev"]
    if crossed_up:
        points += 7; reasons.append("주봉 MACD 골든크로스")
    elif crossed_down:
        points -= 7; reasons.append("주봉 MACD 데드크로스")
    elif weekly["macd_hist"] > weekly["macd_hist_prev"] > weekly["macd_hist_prev2"]:
        points += 4; reasons.append("히스토그램 2주 개선")
    elif weekly["macd_hist"] < weekly["macd_hist_prev"] < weekly["macd_hist_prev2"]:
        points -= 4; reasons.append("히스토그램 2주 악화")
    else:
        reasons.append("MACD 전환 미확인")
    suffix = " (진행 주 제외)" if weekly.get("current_week_excluded") else ""
    return Factor("weekly_momentum", "주봉 모멘텀", int(clamp(-15, 15, points)), 15, True, " · ".join(reasons) + suffix)


def flow_summary(frame: pd.DataFrame) -> dict[str, Any]:
    foreign = frame["foreign"]
    streak = 0
    for value in reversed(foreign.tolist()):
        sign = 1 if value > 0 else -1 if value < 0 else 0
        if sign == 0:
            break
        if streak == 0:
            streak = sign
        elif sign == (1 if streak > 0 else -1):
            streak += sign
        else:
            break
    return {
        "date": frame.index[-1].date().isoformat(),
        "foreign_1d": round(float(foreign.iloc[-1]), 4),
        "institution_1d": round(float(frame["institution"].iloc[-1]), 4),
        "personal_1d": round(float(frame["personal"].iloc[-1]), 4),
        "foreign_3d": round(float(foreign.iloc[-3:].sum()), 4),
        "foreign_5d": round(float(foreign.iloc[-5:].sum()), 4),
        "foreign_10d": round(float(foreign.iloc[-10:].sum()), 4),
        "foreign_streak": streak,
    }


def flow_factor(summary: dict[str, Any] | None) -> Factor:
    if not summary:
        return Factor("foreign_flow", "외국인 수급", 0, 15, False, "수급 데이터 누락")
    three, ten, streak = summary["foreign_3d"], summary["foreign_10d"], summary["foreign_streak"]
    points = 8 if three >= 1 else 4 if three >= 0.2 else -8 if three <= -1 else -4 if three <= -0.2 else 0
    points += 5 if ten >= 3 else 2 if ten >= 0.5 else -5 if ten <= -3 else -2 if ten <= -0.5 else 0
    if streak >= 3:
        points += 2
    elif streak <= -3:
        points -= 2
    stability_note = ""
    if three > 0 and ten < 0 and streak <= 0:
        points = min(points, 0)
        stability_note = " · 10일 누적 음수·연속 매수 미확인"
    reason = (
        f"외국인 3일 {three:+.2f}조 · 10일 {ten:+.2f}조 · 연속 {streak:+d}일"
        + stability_note
    )
    return Factor("foreign_flow", "외국인 수급", int(clamp(-15, 15, points)), 15, True, reason)


def scalar_metric(series: pd.Series, name: str) -> dict[str, Any]:
    clean = series.dropna()
    last = float(clean.iloc[-1])
    previous = float(clean.iloc[-2])
    ma20 = clean.rolling(20).mean()
    return {
        "symbol": name,
        "date": clean.index[-1].date().isoformat(),
        "close": round(last, 4),
        "change_1d_pct": round((last / previous - 1) * 100, 3),
        "change_5d_pct": round(pct_change(clean, 5) or 0.0, 3),
        "ma20": round(float(ma20.iloc[-1]), 4) if pd.notna(ma20.iloc[-1]) else None,
        "ma20_slope": round(float(ma20.iloc[-1] - ma20.iloc[-6]), 4) if len(ma20.dropna()) >= 6 else None,
    }


def fx_factor(metric: dict[str, Any] | None) -> Factor:
    if not metric:
        return Factor("fx", "원/달러", 0, 10, False, "환율 데이터 누락")
    change = metric["change_5d_pct"]
    points = 6 if change <= -2 else 3 if change <= -0.5 else -6 if change >= 2 else -3 if change >= 0.5 else 0
    if metric.get("ma20") is not None and metric.get("ma20_slope") is not None:
        if metric["close"] < metric["ma20"] and metric["ma20_slope"] < 0:
            points += 4
        elif metric["close"] > metric["ma20"] and metric["ma20_slope"] > 0:
            points -= 4
    reason = f"USD/KRW 5일 {change:+.2f}% · 현재 {metric['close']:,.1f}원"
    return Factor("fx", "원/달러", int(clamp(-10, 10, points)), 10, True, reason)


def semiconductor_factor(
    soxx: dict[str, Any] | None,
    samsung: dict[str, Any] | None,
    hynix: dict[str, Any] | None,
    kospi: dict[str, Any] | None,
) -> Factor:
    if not soxx:
        return Factor("semiconductor", "반도체", 0, 10, False, "SOXX 데이터 누락")
    change = soxx["change_5d_pct"]
    points = 5 if change > 5 else 3 if change > 1 else -5 if change < -5 else -3 if change < -1 else 0
    reasons = [f"SOXX 5일 {change:+.2f}%"]
    if samsung and hynix and kospi:
        relative = (samsung["change_5d_pct"] + hynix["change_5d_pct"]) / 2 - kospi["change_5d_pct"]
        if relative > 2:
            points += 3
        elif relative < -2:
            points -= 3
        reasons.append(f"한국 반도체 상대강도 {relative:+.2f}%p")
    if soxx["close"] >= soxx["ma20"]:
        points += 2; reasons.append("SOXX 20일선 상단")
    else:
        points -= 2; reasons.append("SOXX 20일선 하단")
    coverage_points = 10 if samsung and hynix and kospi else 7
    return Factor(
        "semiconductor",
        "반도체",
        int(clamp(-10, 10, points)),
        10,
        True,
        " · ".join(reasons),
        coverage_points,
    )


def macro_factor(
    vix: dict[str, Any] | None,
    dollar: dict[str, Any] | None,
    us10y: dict[str, Any] | None,
) -> Factor:
    available = any((vix, dollar, us10y))
    if not available:
        return Factor("macro_risk", "글로벌 위험", 0, 10, False, "매크로 데이터 누락")
    points, reasons = 0, []
    if vix:
        level, change = vix["close"], vix["change_5d_pct"]
        if level < 25 and change < 0:
            points += 5
        elif level > 35 and change > 0:
            points -= 5
        elif 25 <= level <= 35:
            points += 2 if change < 0 else -2
        reasons.append(f"VIX {level:.1f} ({change:+.1f}%/5D)")
    if dollar:
        change = dollar["change_5d_pct"]
        if change <= -1:
            points += 3
        elif change >= 1:
            points -= 3
        reasons.append(f"달러 5일 {change:+.1f}%")
    if us10y:
        # Rate series is a yield in percent; convert the 5-session difference to bp.
        bp = us10y.get("change_5d_bp", 0.0)
        if bp >= 20:
            points -= 2
        elif bp <= -20:
            points += -2 if vix and vix["change_5d_pct"] > 0 else 2
        reasons.append(f"미10년 {us10y['close']:.2f}% ({bp:+.0f}bp/5D)")
    coverage_points = (5 if vix else 0) + (3 if dollar else 0) + (2 if us10y else 0)
    return Factor(
        "macro_risk",
        "글로벌 위험",
        int(clamp(-10, 10, points)),
        10,
        True,
        " · ".join(reasons),
        coverage_points,
    )


def wave_factor(frame: pd.DataFrame, ewy: dict[str, Any]) -> tuple[Factor, dict[str, Any]]:
    recent_low = float(frame.loc[frame.index >= pd.Timestamp("2026-06-01"), "low"].dropna().min())
    a_length = WAVE["high"] - WAVE["a_low"]
    ratio = (WAVE["b_high"] - recent_low) / a_length
    if recent_low < LEVELS["deep_support"]:
        points, reason = -5, "135.48 이탈로 수동 ABC 카운트 무효"
    elif 0.786 <= ratio <= 1.0 and ewy["close"] >= WAVE["fib_support"]:
        points, reason = 5, "C/A가 0.786~1.0이고 155.58 회복"
    elif 0.618 <= ratio <= 1.272:
        points, reason = 2, "C/A가 통상 조정 범위 0.618~1.272"
    elif ratio > 1.272:
        points, reason = -3, "C파 1.272배 확장"
    else:
        points, reason = 0, "파동 완성도 중립"
    wave = {
        "anchors": WAVE,
        "manual_anchor": True,
        "cycle_low": round(recent_low, 2),
        "c_to_a_ratio": round(ratio, 3),
        "primary_count": "220.09 → 174.45(A) → 192.25(B) → 진행 중 C",
        "interpretation": reason,
        "invalidation": "주봉 153~155 하회 시 145.81, 이후 135.48 위험 확대",
    }
    return Factor("wave", "파동 완성도", points, 5, True, reason), wave


def verdict_label(near_end: int) -> str:
    if near_end >= 68:
        return "막바지 가능성 높음"
    if near_end >= 56:
        return "막바지 우세"
    if near_end >= 45:
        return "재시험·방향 확인 구간"
    if near_end >= 33:
        return "조정 진행 중 우세"
    return "추가 조정 가능성 높음"


def scenario_probabilities(score: int) -> list[dict[str, Any]]:
    direct = int(round(clamp(15, 70, 35 + 0.30 * score)))
    deep = int(round(clamp(5, 45, 15 - 0.18 * score)))
    retest = 100 - direct - deep
    return [
        {"key": "direct", "label": "저점 형성·직접 반등", "probability": direct, "condition": "169~177.5 회복 후 higher low"},
        {"key": "retest", "label": "154~156 재시험·바닥 다지기", "probability": retest, "condition": "지지 재시험 시 거래량 감소"},
        {"key": "deep", "label": "145~135 심화 조정", "probability": deep, "condition": "주봉 153 하회 시 위험 확대"},
    ]


def level_rows(price: float) -> list[dict[str, Any]]:
    specs = [
        (135.48, "심화 방어", "risk"),
        (145.81, "ABC 1:1 목표", "risk"),
        (153.00, "바닥 가설 무효화", "support"),
        (155.58, "핵심 피보나치 지지", "support"),
        (169.00, "1차 회복 구간", "confirmation"),
        (172.00, "1차 회복 상단", "confirmation"),
        (177.50, "추세 반등 확인", "confirmation"),
        (184.20, "주봉 강한 확인", "confirmation"),
        (192.25, "ABC 하락 종료 확인", "confirmation"),
    ]
    rows = []
    for value, label, kind in specs:
        distance = (value / price - 1) * 100
        if abs(distance) <= 2.5:
            state = "접근"
        elif value < price:
            state = "상단 유지" if kind == "support" else "돌파"
        else:
            state = "미도달"
        rows.append(
            {
                "value": value,
                "label": label,
                "kind": kind,
                "distance_pct": round(distance, 2),
                "state": state,
            }
        )
    return rows


def signal_rows(
    ewy: dict[str, Any], weekly: dict[str, Any], flows: dict[str, Any] | None
) -> list[dict[str, str]]:
    price = ewy["close"]
    support_state = "충족" if price >= 155.58 else "진행" if price >= 153 else "무효"
    return [
        {"key": "support", "label": "주봉 153~155 방어", "state": support_state, "detail": f"EWY {price:.2f}"},
        {"key": "recovery", "label": "169~172 종가 회복", "state": "충족" if price >= 169 else "진행" if price >= 164 else "미충족", "detail": "1차 매도압력 완화"},
        {"key": "foreign", "label": "외국인 현물 3일 연속 순매수", "state": "충족" if flows and flows["foreign_streak"] >= 3 else "미충족", "detail": f"현재 {flows['foreign_streak']:+d}일" if flows else "데이터 누락"},
        {"key": "momentum", "label": "주봉 RSI·MACD 동시 개선", "state": "충족" if weekly["rsi14"] >= weekly["rsi14_prev"] and weekly["macd_hist"] > weekly["macd_hist_prev"] else "미충족", "detail": f"RSI {weekly['rsi14']:.1f} · Hist {weekly['macd_hist']:+.2f}"},
        {"key": "trend", "label": "177.5 추세 확인선 돌파", "state": "충족" if price >= 177.5 else "미충족", "detail": f"거리 {(177.5 / price - 1) * 100:+.1f}%"},
        {"key": "strong", "label": "184.2 주봉 강한 확인", "state": "충족" if price >= 184.2 else "미충족", "detail": f"거리 {(184.2 / price - 1) * 100:+.1f}%"},
    ]


def df_series(frame: pd.DataFrame, limit: int, fields: tuple[str, ...] = ("close",)) -> list[dict[str, Any]]:
    rows = []
    for idx, row in frame.tail(limit).iterrows():
        item: dict[str, Any] = {"date": idx.date().isoformat()}
        for field in fields:
            value = row.get(field)
            item[field] = None if pd.isna(value) else round(float(value), 4)
        rows.append(item)
    return rows


def scalar_series(series: pd.Series, limit: int) -> list[dict[str, Any]]:
    return [
        {"date": idx.date().isoformat(), "close": round(float(value), 4)}
        for idx, value in series.dropna().tail(limit).items()
    ]


def compare_factor_changes(previous: dict[str, Any] | None, factors: list[Factor]) -> list[str]:
    if not previous:
        return ["초기 기준선 생성"]
    old = {item["key"]: item for item in previous.get("factors", [])}
    changes = []
    for factor in factors:
        before = old.get(factor.key, {}).get("points")
        if isinstance(before, (int, float)) and before != factor.points:
            changes.append((abs(factor.points - before), f"{factor.label} {before:+g}→{factor.points:+d}"))
    changes.sort(reverse=True)
    return [text for _, text in changes[:3]] or ["핵심 점수 변화 없음"]


def compact_history(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "market_key": snapshot["market_key"],
        "generated_at": snapshot["generated_at"],
        "market_dates": snapshot["market_dates"],
        "status": snapshot["status"],
        "verdict": snapshot["verdict"],
        "scenarios": snapshot["scenarios"],
        "metrics": {
            key: snapshot["metrics"].get(key)
            for key in ("EWY", "KOSPI", "USDKRW", "SOXX")
        },
        "flows": snapshot.get("flows", {}).get("summary"),
        "signal_summary": snapshot.get("signal_summary"),
        "factors": snapshot.get("factors", []),
        "reasons": snapshot.get("reasons", {}),
        "change_reasons": snapshot["change_reasons"],
    }


def upsert_history(history: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    observations = history.get("observations", []) if isinstance(history, dict) else []
    compact = compact_history(snapshot)
    replaced = False
    for idx, item in enumerate(observations):
        if item.get("market_key") == compact["market_key"]:
            observations[idx] = compact
            replaced = True
            break
    if not replaced:
        observations.append(compact)
    observations = sorted(observations, key=lambda x: x.get("generated_at", ""))[-730:]
    return {
        "schema_version": SCHEMA_VERSION,
        "methodology_version": METHODOLOGY_VERSION,
        "updated_at": snapshot["generated_at"],
        "observations": observations,
    }


def build_methodology() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "version": METHODOLOGY_VERSION,
        "title": "규칙 기반 조정 막바지 추정",
        "probability_note": "통계적으로 보정된 예측확률이 아니라 공개 규칙의 증거점수를 5~95% 범위로 변환한 추적지표입니다.",
        "scenario_note": "세 경로의 비중은 단기 전개를 따로 나눈 조건부 추정이며, 막바지/진행 중 확률과 합산하거나 동일시하지 않습니다.",
        "formula": "near_end_probability = clamp(5, 95, 50 + 0.45 × evidence_score)",
        "factors": [
            {"key": "price_structure", "label": "가격 구조", "weight": 20},
            {"key": "daily_reversal", "label": "단기 반전", "weight": 15},
            {"key": "weekly_momentum", "label": "주봉 RSI·MACD", "weight": 15},
            {"key": "foreign_flow", "label": "외국인 현물 수급", "weight": 15},
            {"key": "fx", "label": "원/달러", "weight": 10},
            {"key": "semiconductor", "label": "한·미 반도체", "weight": 10},
            {"key": "macro_risk", "label": "VIX·달러·미10년", "weight": 10},
            {"key": "wave", "label": "수동 ABC 앵커", "weight": 5},
        ],
        "limitations": [
            "EWY는 코스피 자체가 아니라 한국 주식과 USD/KRW를 함께 반영합니다.",
            "엘리엇 파동 앵커는 주관적이므로 전체 점수의 5%만 반영합니다.",
            "한국과 미국의 휴장일이 다르므로 시장별 기준일을 따로 표시합니다.",
            "수급·가격 공급자의 지연 또는 정정 가능성이 있어 최신 성공일과 신뢰도를 함께 봐야 합니다.",
        ],
        "levels": LEVELS,
    }


def collect_snapshot(previous: dict[str, Any] | None = None) -> dict[str, Any]:
    session = http_session()
    errors: list[str] = []
    warnings: list[str] = []
    cache = load_macro_cache()
    sources: list[dict[str, Any]] = []

    ewy_history, source = fetch_nasdaq_history(session, "EWY")
    sources.append(source)
    ewy_current = fetch_nasdaq_current(session, "EWY")
    ewy_frame = merge_current_observation(ewy_history, ewy_current, EWY_BOOTSTRAP)
    if ewy_frame.index[-1].date().isoformat() == EWY_BOOTSTRAP["date"] and float(ewy_frame["close"].iloc[-1]) == EWY_BOOTSTRAP["close"]:
        sources.append(
            {
                "name": "EWY initial chart baseline",
                "url": "",
                "as_of": EWY_BOOTSTRAP["date"],
                "status": "bootstrap_until_history_updates",
            }
        )

    kospi_frame, source = fetch_naver_index(session)
    sources.append(source)

    try:
        flow_frame, source = fetch_naver_flows(session, kospi_frame.index, 20)
        sources.append(source)
        flows = flow_summary(flow_frame)
    except Exception as exc:
        flow_frame, flows = pd.DataFrame(), None
        errors.append(f"investor_flow: {exc}")

    try:
        soxx_history, source = fetch_nasdaq_history(session, "SOXX")
        sources.append(source)
        soxx_frame = merge_current_observation(soxx_history, fetch_nasdaq_current(session, "SOXX"))
    except Exception as exc:
        soxx_frame = pd.DataFrame()
        errors.append(f"SOXX: {exc}")

    try:
        samsung_frame = fetch_naver_stock(session, "005930")
        hynix_frame = fetch_naver_stock(session, "000660")
        sources.append(
            {
                "name": "Naver domestic equities",
                "url": "https://m.stock.naver.com/domestic/stock/005930/total",
                "as_of": max(samsung_frame.index[-1], hynix_frame.index[-1]).date().isoformat(),
                "status": "ok",
            }
        )
    except Exception as exc:
        samsung_frame = hynix_frame = pd.DataFrame()
        errors.append(f"Korean semiconductors: {exc}")

    fred_data: dict[str, pd.Series] = {}
    for series_id in ("VIXCLS", "DGS10", "DTWEXBGS"):
        try:
            fred_data[series_id], source = fetch_macro_series(session, series_id, warnings, cache)
            sources.append(source)
        except Exception as exc:
            errors.append(f"FRED {series_id}: {exc}")

    try:
        fx_series, source = fetch_naver_fx(session)
        sources.append(source)
    except Exception as exc:
        errors.append(f"Naver USD/KRW: {exc}")
        try:
            fx_series, source = fetch_macro_series(session, "DEXKOUS", warnings, cache)
            sources.append(source)
        except Exception as fallback_exc:
            fx_series = pd.Series(dtype="float64")
            errors.append(f"FRED DEXKOUS: {fallback_exc}")
    json_dump(MACRO_CACHE_PATH, cache)   # 라이브 실측 보관 (data/*.json 화이트리스트에 포함)

    fx_current_source = None
    if not fx_series.empty:
        fx_series, fx_current_source = append_current_fx(session, fx_series)
        if fx_current_source:
            sources.append(fx_current_source)

    ewy = series_metric(ewy_frame, "EWY")
    kospi = series_metric(kospi_frame, "KOSPI")
    weekly = weekly_metric(ewy_frame)
    soxx = series_metric(soxx_frame, "SOXX") if not soxx_frame.empty else None
    samsung = series_metric(samsung_frame, "Samsung Electronics") if not samsung_frame.empty else None
    hynix = series_metric(hynix_frame, "SK hynix") if not hynix_frame.empty else None
    fx = scalar_metric(fx_series, "USD/KRW") if not fx_series.empty else None
    vix = scalar_metric(fred_data["VIXCLS"], "VIX") if "VIXCLS" in fred_data else None
    dollar = scalar_metric(fred_data["DTWEXBGS"], "Broad USD") if "DTWEXBGS" in fred_data else None
    us10y = scalar_metric(fred_data["DGS10"], "US10Y") if "DGS10" in fred_data else None
    if us10y and "DGS10" in fred_data and len(fred_data["DGS10"].dropna()) >= 6:
        yields = fred_data["DGS10"].dropna()
        us10y["change_5d_bp"] = round(float((yields.iloc[-1] - yields.iloc[-6]) * 100), 2)

    factors = [
        price_factor(ewy),
        daily_factor(ewy_frame),
        weekly_factor(weekly),
        flow_factor(flows),
        fx_factor(fx),
        semiconductor_factor(soxx, samsung, hynix, kospi),
        macro_factor(vix, dollar, us10y),
    ]
    wave_score, wave = wave_factor(ewy_frame, ewy)
    factors.append(wave_score)

    score = sum(item.points for item in factors)
    available_weight = sum(
        item.coverage_points if item.coverage_points is not None else item.max_points
        for item in factors
        if item.available
    )
    coverage = int(round(available_weight))
    near_end = int(round(clamp(5, 95, 50 + 0.45 * score)))
    ongoing = 100 - near_end
    confidence = ("높음" if coverage >= 90 and not errors
                  else "보통" if coverage >= 75 else "낮음")
    label = verdict_label(near_end)
    previous_probability = ((previous or {}).get("verdict") or {}).get("near_end_probability")
    change_pp = near_end - previous_probability if isinstance(previous_probability, (int, float)) else 0

    if ewy["close"] < 153:
        summary = "핵심 지지 이탈로 145.8·135.5 하방 위험이 확대됐습니다."
    elif ewy["close"] < 169:
        summary = "가격은 막바지 후보권이지만 주봉 모멘텀과 수급상 바닥은 미확인입니다."
    elif ewy["close"] < 177.5:
        summary = "1차 회복 구간에 진입했지만 higher low 확인이 필요합니다."
    elif ewy["close"] < 184.2:
        summary = "반등 추세가 확인되고 있으며 184.2 주봉 회복이 다음 관문입니다."
    else:
        summary = "주봉 강한 확인선을 회복해 조정 종료 신뢰도가 높아졌습니다."

    bull = [item.reason for item in sorted(factors, key=lambda x: x.points, reverse=True) if item.points > 0][:3]
    bear = [item.reason for item in sorted(factors, key=lambda x: x.points) if item.points < 0][:3]
    market_dates = {
        "us": ewy["date"],
        "kr": kospi["date"],
        "flow": flows["date"] if flows else None,
        "fx": fx["date"] if fx else None,
        "macro": vix["date"] if vix else None,
        "soxx": soxx["date"] if soxx else None,
        "samsung": samsung["date"] if samsung else None,
        "hynix": hynix["date"] if hynix else None,
        "dollar": dollar["date"] if dollar else None,
        "us10y": us10y["date"] if us10y else None,
    }
    previous_dates = (previous or {}).get("market_dates") or {}
    for key in ("us", "kr", "flow"):
        old, new = previous_dates.get(key), market_dates.get(key)
        if old and new and date.fromisoformat(new) < date.fromisoformat(old):
            raise DataError(f"{key} market date moved backwards: {old} -> {new}")
    market_key = f"US-{market_dates['us']}_KR-{market_dates['kr']}"

    freshness = []
    today = now_kst().date()
    freshness_thresholds = {
        "us": 4,
        "kr": 4,
        "flow": 4,
        "fx": 4,
        "macro": 7,
        "soxx": 4,
        "samsung": 4,
        "hynix": 4,
        "dollar": 10,
        "us10y": 7,
    }
    for key, threshold in freshness_thresholds.items():
        value = market_dates.get(key)
        if value:
            age = (today - date.fromisoformat(value)).days
            freshness.append({"market": key, "as_of": value, "age_days": age, "stale": age > threshold})
    stale = any(item["stale"] for item in freshness)
    status_code = "STALE" if stale else "DEGRADED" if errors or coverage < 90 else "OK"

    signals = signal_rows(ewy, weekly, flows)
    met_signals = sum(item["state"] == "충족" for item in signals)
    generated = now_kst().isoformat(timespec="seconds")
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "methodology_version": METHODOLOGY_VERSION,
        "generated_at": generated,
        "market_key": market_key,
        "market_dates": market_dates,
        "status": {
            "code": status_code,
            "coverage_pct": coverage,
            "confidence": confidence,
            "message": "모든 핵심 데이터 정상" if status_code == "OK" else "일부 데이터 지연 또는 누락 — 신뢰도와 기준일 확인",
            "errors": errors,
            "warnings": warnings,
            "freshness": freshness,
        },
        "verdict": {
            "label": label,
            "score": score,
            "near_end_probability": near_end,
            "ongoing_probability": ongoing,
            "change_pp": change_pp,
            "summary": summary,
            "confidence": confidence,
            "probability_type": "rules_based_estimate",
        },
        "scenarios": scenario_probabilities(score),
        "scenario_note": "단기 경로 추정은 현재 상태의 막바지/진행 중 확률과 별도입니다.",
        "metrics": {
            "EWY": ewy,
            "EWY_WEEKLY": weekly,
            "KOSPI": kospi,
            "USDKRW": fx,
            "SOXX": soxx,
            "SAMSUNG": samsung,
            "HYNIX": hynix,
            "VIX": vix,
            "US10Y": us10y,
            "DOLLAR": dollar,
        },
        "flows": {
            "summary": flows,
            "series": df_series(flow_frame, 20, ("foreign", "institution", "personal")) if not flow_frame.empty else [],
        },
        "wave": wave,
        "levels": level_rows(ewy["close"]),
        "signals": signals,
        "signal_summary": {"met": met_signals, "total": len(signals)},
        "factors": [item.as_dict() for item in factors],
        "reasons": {"bull": bull, "bear": bear},
        "change_reasons": compare_factor_changes(previous, factors),
        "series": {
            "EWY": df_series(ewy_frame, 260, ("close", "volume")),
            "KOSPI": df_series(kospi_frame, 120, ("close",)),
            "SOXX": df_series(soxx_frame, 120, ("close",)) if not soxx_frame.empty else [],
            "USDKRW": scalar_series(fx_series, 120) if not fx_series.empty else [],
        },
        "sources": sources,
        "disclaimer": "규칙 기반 시장 모니터링 자료이며 투자자문이 아닙니다.",
    }
    return snapshot


def build_telegram_message(snapshot: dict[str, Any]) -> str:
    verdict = snapshot["verdict"]
    ewy = snapshot["metrics"]["EWY"]
    weekly = snapshot["metrics"]["EWY_WEEKLY"]
    fx = snapshot["metrics"].get("USDKRW")
    soxx = snapshot["metrics"].get("SOXX")
    flows = snapshot.get("flows", {}).get("summary")
    scenarios = {item["key"]: item["probability"] for item in snapshot["scenarios"]}
    signal = snapshot["signal_summary"]

    lines = [
        "🧭 <b>EWY·코스피 조정 모니터</b>",
        f"<i>{html.escape(snapshot['market_dates']['us'])} 미국장 · {html.escape(snapshot['market_dates']['kr'])} 한국장</i>",
    ]
    if snapshot["status"]["code"] != "OK":
        lines.append(
            f"⚠️ 데이터 상태 {snapshot['status']['code']} · 기준일·누락 항목을 대시보드에서 확인"
        )
    lines += [
        "",
        f"🟠 <b>{html.escape(verdict['label'])}</b>",
        f"막바지 {verdict['near_end_probability']}% · 진행 {verdict['ongoing_probability']}% · 데이터 {snapshot['status']['coverage_pct']}% · 신뢰도 {snapshot['status']['confidence']}",
        f"단기 경로(별도): 직접반등 {scenarios['direct']}% · 재시험 {scenarios['retest']}% · 심화 {scenarios['deep']}%",
        "",
        f"EWY <b>{ewy['close']:.2f}</b> ({ewy['change_1d_pct']:+.2f}%) · 155.58 지지까지 {(155.58 / ewy['close'] - 1) * 100:+.1f}%",
    ]
    if flows:
        lines.append(f"외국인 1D {flows['foreign_1d']:+.2f}조 · 3D {flows['foreign_3d']:+.2f}조 · 10D {flows['foreign_10d']:+.2f}조")
    lines.append(f"주봉 RSI {weekly['rsi14']:.1f} · MACD Hist {weekly['macd_hist']:+.2f}")
    if fx or soxx:
        context = []
        if fx:
            context.append(f"원달러 {fx['close']:,.1f} ({fx['change_5d_pct']:+.1f}%/5D)")
        if soxx:
            context.append(f"SOXX {soxx['change_5d_pct']:+.1f}%/5D")
        lines.append(" · ".join(context))
    lines += [
        f"확인 신호 {signal['met']}/{signal['total']} · 153↓ 위험 / 177.5↑ 개선 / 184.2↑ 강한 확인",
        "",
        f'📊 <a href="{PAGE_URL}">상세 이력 대시보드</a>',
        "<i>규칙 기반 추정 · 투자자문 아님</i>",
    ]
    text = "\n".join(lines)
    if len(text) > 4096:
        raise ValueError("Telegram message exceeds 4096 characters")
    return text


def send_telegram(message: str, token: str, chat_id: str) -> None:
    if not token or not chat_id:
        raise DataError("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID is missing")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    last_error = "unknown error"
    for attempt in range(4):
        try:
            response = requests.post(url, json=payload, timeout=25)
        except requests.RequestException as exc:
            last_error = type(exc).__name__
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            break
        if response.status_code == 429 and attempt < 3:
            try:
                wait = int(response.json().get("parameters", {}).get("retry_after", 2))
            except Exception:
                wait = 2
            time.sleep(min(wait, 30))
            continue
        if response.status_code >= 500 and attempt < 3:
            last_error = f"HTTP {response.status_code}"
            time.sleep(2 ** attempt)
            continue
        if response.ok:
            return
        raise DataError(f"Telegram returned HTTP {response.status_code}")
    raise DataError(f"Telegram send failed after retries ({last_error})")


def record_successful_notification(snapshot: dict[str, Any]) -> None:
    json_dump(
        NOTIFICATION_PATH,
        {
            "schema_version": SCHEMA_VERSION,
            "market_key": snapshot["market_key"],
            "notified_at": now_kst().isoformat(timespec="seconds"),
        },
    )


def wait_for_pages(market_key: str, timeout: int, generated_at: str | None = None) -> None:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            response = requests.get(PAGE_DATA_URL, params={"v": int(time.time())}, timeout=15)
            response.raise_for_status()
            remote = response.json()
            key_matches = remote.get("market_key") == market_key
            timestamp_matches = generated_at is None or remote.get("generated_at") == generated_at
            if key_matches and timestamp_matches:
                print(f"[ok] Pages contains {market_key} ({remote.get('generated_at')})")
                return
            last_error = "snapshot key/timestamp not updated"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(10)
    raise DataError(f"GitHub Pages did not publish {market_key} within {timeout}s: {last_error}")


def write_github_output(path: str | None, values: dict[str, Any]) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Collect/write data but never send Telegram")
    parser.add_argument("--send-existing", action="store_true", help="Send Telegram from data/latest.json without fetching")
    parser.add_argument("--force-send", action="store_true", help="Allow notification even when market dates did not change")
    parser.add_argument("--github-output", default="", help="Optional GITHUB_OUTPUT file")
    parser.add_argument("--wait-pages", metavar="MARKET_KEY", help="Poll the public Pages JSON until this key appears")
    parser.add_argument("--generated-at", help="With --wait-pages, require this exact generated_at value")
    parser.add_argument("--timeout", type=int, default=240)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.wait_pages:
        wait_for_pages(args.wait_pages, args.timeout, args.generated_at)
        return

    if args.send_existing:
        snapshot = load_json(LATEST_PATH, None)
        if not snapshot:
            raise DataError("data/latest.json is missing")
        message = build_telegram_message(snapshot)
        send_telegram(message, os.environ.get("TELEGRAM_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", ""))
        record_successful_notification(snapshot)
        print("[ok] Telegram sent from existing snapshot")
        return

    previous = load_json(LATEST_PATH, None)
    notification_state = load_json(NOTIFICATION_PATH, {})
    last_notified_key = notification_state.get("market_key")
    snapshot = collect_snapshot(previous)
    history = upsert_history(load_json(HISTORY_PATH, {}), snapshot)
    json_dump(LATEST_PATH, snapshot)
    json_dump(HISTORY_PATH, history)
    json_dump(METHODOLOGY_PATH, build_methodology())
    notification_needed = last_notified_key != snapshot["market_key"]
    write_github_output(
        args.github_output,
        {
            "market_key": snapshot["market_key"],
            "notification_needed": notification_needed,
            "status_code": snapshot["status"]["code"],
        },
    )
    message = build_telegram_message(snapshot)
    print(message)
    print(f"\n[ok] wrote {LATEST_PATH.relative_to(ROOT)} and history ({len(history['observations'])} snapshots)")

    if not args.dry_run and (notification_needed or args.force_send):
        send_telegram(message, os.environ.get("TELEGRAM_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", ""))
        record_successful_notification(snapshot)
        print("[ok] Telegram sent")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
