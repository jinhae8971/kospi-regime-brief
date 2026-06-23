#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Daily Strategy Regime Brief -> Telegram
---------------------------------------
Tracks the day-over-day macro / semiconductor signature against the working thesis:
"Is the current KOSPI correction a V-recovery (like the 2026 exogenous shocks) or a
 regime-shift lower-high (Fed cuts->hikes + endogenous valuation unwind)?"

Runs every weekday 06:00 KST via GitHub Actions (cron handles weekend exclusion).
Auth comes from env vars (GitHub Secrets); config.json is a local-test fallback.

Usage:
  python daily_brief.py            # fetch + send to Telegram
  python daily_brief.py --dry-run  # fetch + print only (no send)
"""

import os
import sys
import json
import math
from datetime import datetime, timezone, timedelta

import requests

KST = timezone(timedelta(hours=9))
WD_KR = ["월", "화", "수", "목", "금", "토", "일"]

# KOSPI all-time-high close (2026-06-22). Used as the "distance from peak" anchor.
KOSPI_ATH = 9114.0

# Link to the 4-correction comparison (served via GitHub Pages from this repo's index.html)
COMPARE_URL = "https://jinhae8971.github.io/kospi-regime-brief/"

# label -> Yahoo ticker
TICKERS = {
    "KOSPI": "^KS11",
    "US10Y": "^TNX",
    "DXY":   "DX-Y.NYB",
    "VIX":   "^VIX",
    "SOX":   "^SOX",
    "MU":    "MU",
    "NVDA":  "NVDA",
    "EWY":   "EWY",
    "KORU":  "KORU",
    "SOXL":  "SOXL",
}


# ----------------------------------------------------------------------------- config
def load_config() -> dict:
    cfg = {
        "telegram_token":   os.environ.get("TELEGRAM_TOKEN", ""),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
    }
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for k, v in json.load(f).items():
                if not cfg.get(k):
                    cfg[k] = v
    return cfg


# ------------------------------------------------------------------------------- data
def fetch_closes(ticker: str, days: int = 12):
    """Return ascending [(date_str, close_float), ...] via yfinance, [] on failure."""
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).history(period=f"{days}d", interval="1d", auto_adjust=False)
        out = []
        for idx, row in df.iterrows():
            c = row.get("Close")
            if c is not None and not (isinstance(c, float) and math.isnan(c)):
                out.append((idx.strftime("%Y-%m-%d"), float(c)))
        return out
    except Exception as e:
        print(f"[warn] fetch {ticker}: {e}", file=sys.stderr)
        return []


def norm_yield(v: float) -> float:
    """^TNX is quoted either as percent (4.50) or x10 (45.0). Normalize to percent."""
    return v / 10.0 if v > 20 else v


def compute(label: str, closes: list):
    """Build a metric dict, or None if not enough data."""
    if len(closes) < 2:
        return None
    last, prev = closes[-1][1], closes[-2][1]
    pct = (last / prev - 1.0) * 100.0
    tail = closes[-6:] if len(closes) >= 6 else closes
    arrows = "".join("▲" if tail[i][1] >= tail[i - 1][1] else "▼"
                     for i in range(1, len(tail)))
    m = {"label": label, "last": last, "prev": prev, "pct": pct, "arrows": arrows}
    if label == "KOSPI":
        m["hi"] = max(c for _, c in closes)
    if label == "US10Y":
        m["last_y"] = norm_yield(last)
        m["bp"] = (norm_yield(last) - norm_yield(prev)) * 100.0
    return m


# --------------------------------------------------------------------------- analysis
def regime_verdict(M: dict):
    """Transparent rules-based call. Returns (emoji, text, basis_factors)."""
    score, basis = 0, []
    if M.get("SOX"):
        s = M["SOX"]["pct"]; score += 1 if s > 0 else -1
        basis.append(f"SOX {s:+.1f}%")
    if M.get("VIX"):
        v = M["VIX"]["pct"]; score += 1 if v < 0 else -1
        basis.append(f"VIX {v:+.1f}%")
    if M.get("MU"):
        score += 1 if M["MU"]["pct"] > 0 else -1
    if score >= 2:
        return "🟢", "위험선호 회복 — V 시나리오 우호", basis
    if score <= -2:
        return "🔴", "디리스킹 지속 — lower-high 경계", basis
    return "🟡", "혼조 — 방향성 부재, 관망", basis


def rate_signature(M: dict):
    """The key distinction from our analysis: rate-shock vs safety-bid vs neutral."""
    u, vix = M.get("US10Y"), M.get("VIX")
    if not u:
        return "금리 데이터 없음"
    bp = u["bp"]
    vix_up = bool(vix and vix["pct"] > 0)
    if bp >= 5:
        return f"금리 +{bp:.0f}bp · 상승 압력(멀티플 천장)"
    if bp <= -5 and vix_up:
        return f"금리 {bp:.0f}bp · 안전자산 회피(청산형)"
    if bp <= -5:
        return f"금리 {bp:.0f}bp · 완화적"
    return f"금리 {bp:+.0f}bp · 중립권"


# ---------------------------------------------------------------------------- message
def fmt_close(label: str, m: dict) -> str:
    if label == "US10Y":
        return f"{m['last_y']:.2f}%"
    if m["last"] >= 1000:
        return f"{m['last']:,.1f}"
    return f"{m['last']:,.2f}"


def fmt_chg(label: str, m: dict) -> str:
    if label == "US10Y":
        return f"{m['bp']:+.0f}bp"
    return f"{m['pct']:+.2f}%"


def build_message(M: dict) -> str:
    now = datetime.now(KST)
    datestr = f"{now:%Y-%m-%d} ({WD_KR[now.weekday()]})"

    kospi = M.get("KOSPI"); sox = M.get("SOX")
    koru = M.get("KORU"); vix = M.get("VIX")
    kpct = kospi["pct"] if kospi else None
    spct = sox["pct"] if sox else None

    # --- Q1: did a meaningful correction happen? (KOSPI <= -2.5% OR overnight SOX <= -3%)
    triggers = []
    if kpct is not None and kpct <= -2.5:
        triggers.append("KOSPI")
    if spct is not None and spct <= -3.0:
        triggers.append("밤사이 美반도체")
    if triggers:
        q1 = f"🔴 발생 — {' · '.join(triggers)}"
    else:
        q1 = "🟢 특이사항 없음 (정상 범위)"

    if kospi:
        ath = max(KOSPI_ATH, kospi.get("hi", kospi["last"]))
        gap = (kospi["last"] / ath - 1.0) * 100.0
        kline = f"KOSPI {kpct:+.2f}% · 고점대비 {gap:+.1f}%"
    else:
        kline = "KOSPI 데이터 없음"
    night = " · ".join(x for x in [
        f"SOX {spct:+.1f}%" if sox else None,
        f"KORU {koru['pct']:+.1f}%" if koru else None,
        f"VIX {vix['pct']:+.1f}%" if vix else None,
    ] if x)

    # --- Q2: is it different from the prior 2026 corrections?
    emoji, verdict, _ = regime_verdict(M)
    rate_line = rate_signature(M)

    parts = [
        "🧭 <b>레짐 브리핑</b>",
        f"<i>{datestr} · KST 06:00</i>",
        "",
        "<b>① 전일 의미있는 조정?</b>",
        q1,
        kline,
    ]
    if night:
        parts.append(f"밤사이: {night}")
    parts += [
        "",
        "<b>② 기존과 다른가?</b>",
        "⚠️ 구조적으로 다름 — 6/17 연준 인하→인상 후 쿠션 부재",
        f"오늘 지문: {rate_line}",
        f"방향: {emoji} {verdict}",
        "",
        f'📊 <a href="{COMPARE_URL}">4국면 전체 비교 →</a>',
        "──────────────────",
        "<i>자동 브리핑 · 투자자문 아님</i>",
    ]
    return "\n".join(parts)


# ------------------------------------------------------------------------------- send
def send_telegram(text: str, token: str, chat_id: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(
        url,
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=20,
    )
    r.raise_for_status()


# ------------------------------------------------------------------------------- main
def main():
    dry = "--dry-run" in sys.argv
    M = {}
    for label, ticker in TICKERS.items():
        m = compute(label, fetch_closes(ticker))
        if m:
            M[label] = m

    if not M:
        msg = "⚠️ 전략 브리핑: 데이터 수집 실패 (전 종목). 소스/네트워크 확인 필요."
    else:
        msg = build_message(M)

    if dry:
        print(msg)
        return

    cfg = load_config()
    if not cfg["telegram_token"] or not cfg["telegram_chat_id"]:
        print("[error] Telegram 자격 정보 없음 (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID)", file=sys.stderr)
        sys.exit(1)
    send_telegram(msg, cfg["telegram_token"], cfg["telegram_chat_id"])
    print("[ok] sent")


if __name__ == "__main__":
    main()
