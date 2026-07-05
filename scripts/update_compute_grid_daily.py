#!/usr/bin/env python3
"""Refresh the Compute Grid daily pulse and optional YTD ticker badges."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HTML = ROOT / "compute-grid" / "index.html"
if not DEFAULT_HTML.exists():
    DEFAULT_HTML = ROOT / "index.html"

PRIVATE_LABELS = {
    "Danfoss private",
}

SYMBOL_OVERRIDES = {
    "ABB Ltd ABBN.SW": "ABBN.SW",
    "Ajinomoto 2802.T": "2802.T",
    "Alfa Laval ALFA.ST": "ALFA.ST",
    "Amkor AMKR": "AMKR",
    "ASE Technology ASX": "ASX",
    "Astera Labs ALAB": "ALAB",
    "BE Semiconductor BESI.AS": "BESI.AS",
    "Bel Fuse BELFB": "BELFB",
    "Bloom Energy BE": "BE",
    "Delta Electronics 2308.TW": "2308.TW",
    "Ecolab ECL": "ECL",
    "Eoptolink 300502.SZ": "300502.SZ",
    "Freeport-McMoRan FCX": "FCX",
    "Fuji Electric 6504.T": "6504.T",
    "Furukawa Electric 5801.T": "5801.T",
    "Hanmi Semi 042700.KS": "042700.KS",
    "HD Hyundai Electric 267260.KS": "267260.KS",
    "Hitachi 6501.T": "6501.T",
    "Hyosung Heavy 298040.KS": "298040.KS",
    "Ibiden 4062.T": "4062.T",
    "Infineon IFX.DE": "IFX.DE",
    "Kulicke & Soffa KLIC": "KLIC",
    "Kurita Water 6370.T": "6370.T",
    "Kyocera 6971.T": "6971.T",
    "Linde LIN": "LIN",
    "Lynas LYC.AX": "LYC.AX",
    "MP Materials MP": "MP",
    "Mitsubishi Electric 6503.T": "6503.T",
    "Mitsubishi Heavy 7011.T": "7011.T",
    "Murata 6981.T": "6981.T",
    "Munters MTRS.ST": "MTRS.ST",
    "MYR Group MYRG": "MYRG",
    "Nan Ya PCB 8046.TW": "8046.TW",
    "Organo 6368.T": "6368.T",
    "ROHM 6963.T": "6963.T",
    "Rolls-Royce RR.L": "RR.L",
    "SK Hynix 000660.KS": "000660.KS",
    "Samsung Electro-Mechanics 009150.KS": "009150.KS",
    "Samsung Electronics 005930.KS": "005930.KS",
    "Schneider SU.PA": "SU.PA",
    "Shinko 6967.T": "6967.T",
    "Shin-Etsu 4063.T": "4063.T",
    "Siemens Energy ENR.DE": "ENR.DE",
    "SUMCO 3436.T": "3436.T",
    "Sumitomo Electric 5802.T": "5802.T",
    "TDK 6762.T": "6762.T",
    "Taiyo Yuden 6976.T": "6976.T",
    "Tokyo Electron 8035.T": "8035.T",
    "Tokyo Ohka Kogyo 4186.T": "4186.T",
    "Unimicron 3037.TW": "3037.TW",
    "Veolia VIE.PA": "VIE.PA",
    "Walsin 2492.TW": "2492.TW",
    "Xylem XYL": "XYL",
    "Yageo 2327.TW": "2327.TW",
    "Zhongji Innolight 300308.SZ": "300308.SZ",
}


def label_to_symbol(label: str) -> str | None:
    if label in PRIVATE_LABELS:
        return None
    if label in SYMBOL_OVERRIDES:
        return SYMBOL_OVERRIDES[label]
    if " " in label:
        return label.rsplit(" ", 1)[-1]
    return label


def extract_ytd_map(html: str) -> dict[str, float | None]:
    match = re.search(r"const ytdMap = (\{.*?\n\s*\});", html, re.S)
    if not match:
        raise RuntimeError("Could not find const ytdMap block")
    return json.loads(match.group(1))


def format_js_map(values: dict[str, float | None]) -> str:
    rendered = json.dumps(values, indent=6, sort_keys=True, ensure_ascii=False)
    return f"const ytdMap = {rendered};"


def fetch_ytd(label: str, symbol: str, year: int) -> float | None:
    import yfinance as yf  # type: ignore

    start = f"{year}-01-01"
    data = yf.Ticker(symbol).history(start=start, auto_adjust=False, actions=False)
    if data is None or data.empty:
        return None
    series = data["Adj Close"] if "Adj Close" in data.columns else data["Close"]
    series = series.dropna()
    if len(series) < 2:
        return None
    first = float(series.iloc[0])
    last = float(series.iloc[-1])
    if first <= 0:
        return None
    return round(((last / first) - 1) * 100, 1)


def refresh_ytd(values: dict[str, float | None], year: int, pause: float) -> tuple[dict[str, float | None], list[str]]:
    refreshed: dict[str, float | None] = {}
    failures: list[str] = []
    for label in sorted(values):
        symbol = label_to_symbol(label)
        if symbol is None:
            refreshed[label] = None
            continue
        try:
            value = fetch_ytd(label, symbol, year)
        except Exception as exc:  # keep the daily update resilient
            value = values.get(label)
            failures.append(f"{label} ({symbol}): {exc}")
        refreshed[label] = value
        if pause:
            time.sleep(pause)
    return refreshed, failures


def replace_pulse(html: str, key: str, value: str) -> str:
    pattern = rf'(<b data-pulse="{re.escape(key)}">)(.*?)(</b>)'
    new_html, count = re.subn(pattern, lambda match: f"{match.group(1)}{value}{match.group(3)}", html, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"Could not update pulse field: {key}")
    return new_html


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh Compute Grid daily pulse and YTD badges.")
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML)
    parser.add_argument("--date", default=None, help="Display date, e.g. '05 Jul 2026'. Defaults to Europe/Zurich today.")
    parser.add_argument("--pressure", default="High / stable")
    parser.add_argument("--change", default="Power and interconnection remain the gating layer.")
    parser.add_argument("--market", default="<strong>YTD</strong> badges refreshed from public prices.")
    parser.add_argument("--skip-ytd", action="store_true", help="Update pulse text only.")
    parser.add_argument("--pause", type=float, default=0.05, help="Seconds between yfinance ticker requests.")
    args = parser.parse_args()

    html_path = args.html
    html = html_path.read_text(encoding="utf-8")
    now = datetime.now(ZoneInfo("Europe/Zurich"))
    display_date = args.date or now.strftime("%d %b %Y")

    html = replace_pulse(html, "date", display_date)
    html = replace_pulse(html, "pressure", args.pressure)
    html = replace_pulse(html, "change", args.change)
    html = replace_pulse(html, "market", args.market)
    html = re.sub(r"<span class=\"chip\">Updated [^<]+</span>", f'<span class="chip">Updated {display_date}</span>', html, count=1)

    failures: list[str] = []
    if not args.skip_ytd:
        values = extract_ytd_map(html)
        values, failures = refresh_ytd(values, now.year, args.pause)
        html = re.sub(r"const ytdMap = \{.*?\n\s*\};", format_js_map(values), html, count=1, flags=re.S)

    html_path.write_text(html, encoding="utf-8")
    print(f"Updated {html_path}")
    print(f"Daily pulse date: {display_date}")
    if args.skip_ytd:
        print("YTD refresh: skipped")
    else:
        print(f"YTD refresh: completed with {len(failures)} fallback(s)")
        for failure in failures[:12]:
            print(f"  fallback: {failure}")
        if len(failures) > 12:
            print(f"  ... {len(failures) - 12} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
