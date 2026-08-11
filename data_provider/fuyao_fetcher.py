# -*- coding: utf-8 -*-
"""同花顺金融数据 API (fuyao.aicubes.cn) fetcher for A-share data.

REST contract (verified from https://fuyao.aicubes.cn/llms-full.txt):
  - Daily K:     GET /api/a-share/prices/historical
                   ?thscode=<ticker.SH|SZ>&interval=1d&start=<ms>&end=<ms>&adjust=forward
                 item[]: {date_ms, open_price, high_price, low_price, close_price,
                          volume(股), turnover(元)}
  - Snapshot:    GET /api/a-share/prices/snapshot?thscodes=<a.SH,b.SZ>
                 item[]: {thscode, ticker, last_price, price_change,
                          price_change_ratio_pct, open_price, high_price, low_price,
                          prev_price, volume, turnover}
  - Valuations:  GET /api/a-share/valuations/snapshot?thscodes=...
                 item[]: {ticker, name, pe_ttm, pe_mrq, pb_mrq, ...}
  - Auth:        header `X-api-key: <key>`; envelope {code, message, request_id, data}
  - Base URL:    https://fuyao.aicubes.cn

Environment:
  THS_API_KEY    required; API key issued at the docs site (同花顺账号登录后签发)
  THS_PRIORITY   数据源优先级 (default 0 = try first for A-shares)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pandas as pd
import requests

from .base import (
    BaseFetcher,
    DataFetchError,
    STANDARD_COLUMNS,
    is_bse_code,
    normalize_stock_code,
)
from .realtime_types import UnifiedRealtimeQuote, RealtimeSource

logger = logging.getLogger(__name__)

_BASE_URL = "https://fuyao.aicubes.cn"
_HTTP_TIMEOUT_SECONDS = 10
_MAX_KLINE_BARS = 2000


def _read_ths_api_key() -> str:
    return (os.getenv("THS_API_KEY") or "").strip()


def _read_ths_priority() -> int:
    raw = (os.getenv("THS_PRIORITY") or "0").strip()
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        logger.warning("THS_PRIORITY=%r invalid; defaulting to 0", raw)
        return 0


def _to_ths_symbol(stock_code: str) -> str:
    """Convert DSA code (600519) to thscode (600519.SH / 000001.SZ / 8xxxxx.BJ)."""
    code = normalize_stock_code(stock_code)
    if not code or not code.isdigit() or len(code) != 6:
        return ""
    if is_bse_code(code):
        return f"{code}.BJ"
    if code.startswith(("5", "6", "9")):
        return f"{code}.SH"
    return f"{code}.SZ"


def _date_to_ms(date_text: str) -> int:
    """YYYY-MM-DD -> epoch milliseconds (UTC midnight)."""
    dt = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _ms_to_date_str(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _empty_daily_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=STANDARD_COLUMNS)


def _api_error_message(payload: Dict[str, Any]) -> str:
    if isinstance(payload, dict):
        code = payload.get("code")
        msg = payload.get("message")
        if code not in (None, 0):
            return f"THS code={code} msg={msg}"
    return ""


class FuyaoFetcher(BaseFetcher):
    """同花顺金融数据 API: structured A-share daily K + realtime quote + valuation."""

    name = "FuyaoFetcher"
    priority = 0  # overridden in __init__ from THS_PRIORITY
    allow_empty_daily_data = False

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = (api_key or _read_ths_api_key()).strip()
        self.priority = _read_ths_priority()

    # ------------------------------------------------------------------ #
    # availability
    # ------------------------------------------------------------------ #
    def is_available(self) -> bool:
        return bool(self.api_key)

    is_available_for_request = is_available

    # ------------------------------------------------------------------ #
    # daily K-line (BaseFetcher contract)
    # ------------------------------------------------------------------ #
    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        if not self.api_key:
            raise DataFetchError("FuyaoFetcher: THS_API_KEY not configured")
        symbol = _to_ths_symbol(stock_code)
        if not symbol:
            raise DataFetchError(f"FuyaoFetcher unsupported stock code: {stock_code}")

        params = {
            "thscode": symbol,
            "interval": "1d",
            "start": _date_to_ms(start_date),
            "end": _date_to_ms(end_date),
            "adjust": "forward",
        }
        try:
            response = requests.get(
                f"{_BASE_URL}/api/a-share/prices/historical",
                params=params,
                headers=self._headers(),
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise DataFetchError(f"FuyaoFetcher HTTP error for {stock_code}: {exc}") from exc

        payload = response.json()
        err = _api_error_message(payload)
        if err:
            raise DataFetchError(f"FuyaoFetcher {symbol}: {err}")

        data = payload.get("data") or {}
        items = data.get("item") or []
        if not items:
            logger.info("FuyaoFetcher empty daily history for %s", stock_code)
            return _empty_daily_frame()

        rows = []
        for item in items:
            rows.append(
                {
                    "date": _ms_to_date_str(item.get("date_ms")),
                    "open": item.get("open_price"),
                    "high": item.get("high_price"),
                    "low": item.get("low_price"),
                    "close": item.get("close_price"),
                    "volume": item.get("volume"),
                    "amount": item.get("turnover"),
                }
            )
        df = pd.DataFrame(rows)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
            df = df[(df["date"] >= start_date) & (df["date"] <= end_date)]
        if df.empty:
            return _empty_daily_frame()
        return df

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        normalized = df.copy()
        for column in ("open", "high", "low", "close", "volume", "amount"):
            if column in normalized.columns:
                normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
        if "pct_chg" not in normalized.columns:
            normalized["pct_chg"] = normalized["close"].pct_change().fillna(0.0) * 100
        return normalized[list(STANDARD_COLUMNS)]

    # ------------------------------------------------------------------ #
    # realtime quote (optional override)
    # ------------------------------------------------------------------ #
    def get_unified_realtime_quote(
        self, stock_code: str, prefer: Optional[str] = None
    ) -> Optional[UnifiedRealtimeQuote]:
        if not self.api_key:
            return None
        symbol = _to_ths_symbol(stock_code)
        if not symbol:
            return None
        try:
            response = requests.get(
                f"{_BASE_URL}/api/a-share/prices/snapshot",
                params={"thscodes": symbol},
                headers=self._headers(),
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.debug("FuyaoFetcher snapshot failed for %s: %s", stock_code, exc)
            return None
        payload = response.json()
        if _api_error_message(payload):
            return None
        items = (payload.get("data") or {}).get("item") or []
        if not items:
            return None
        item = items[0]
        return UnifiedRealtimeQuote(
            code=stock_code,
            source=RealtimeSource.TENCENT.value,
            price=item.get("last_price"),
            change_pct=item.get("price_change_ratio_pct"),
            change_amount=item.get("price_change"),
            volume=item.get("volume"),
            amount=item.get("turnover"),
            open_price=item.get("open_price"),
            high=item.get("high_price"),
            low=item.get("low_price"),
            pre_close=item.get("prev_price"),
        )

    # ------------------------------------------------------------------ #
    # valuation snapshot (PE/PB for fundamentals, fail-open)
    # ------------------------------------------------------------------ #
    def get_valuations(self, stock_codes: list[str]) -> Dict[str, Dict[str, Any]]:
        result: Dict[str, Dict[str, Any]] = {}
        if not self.api_key or not stock_codes:
            return result
        symbols = {c: _to_ths_symbol(c) for c in stock_codes}
        valid = [s for s in symbols.values() if s]
        if not valid:
            return result
        try:
            response = requests.get(
                f"{_BASE_URL}/api/a-share/valuations/snapshot",
                params={"thscodes": ",".join(valid)},
                headers=self._headers(),
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.debug("FuyaoFetcher valuations failed: %s", exc)
            return result
        payload = response.json()
        if _api_error_message(payload):
            return result
        items = (payload.get("data") or {}).get("item") or []
        for item in items:
            ticker = str(item.get("ticker") or "")
            for code, symbol in symbols.items():
                if symbol.endswith(f".{ticker}") or str(symbol).startswith(ticker):
                    result[code] = item
                    break
        return result

    def _headers(self) -> Dict[str, str]:
        return {
            "X-api-key": self.api_key,
            "User-Agent": "Mozilla/5.0 (DSA-local)",
            "Accept": "application/json",
        }