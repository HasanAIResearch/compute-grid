#!/usr/bin/env python3
"""Collect, measure, and publish the latest Compute Grid page data.

The page is public research, not advice. This script keeps it alive by:
- extracting the page universe from the current HTML
- fetching public market data for mapped tickers
- collecting public news/RSS headlines for the theme
- measuring category-level changes
- updating the page pulse, YTD map, and project status section
- writing durable JSON/CSV/checkpoint/progress artifacts
"""

from __future__ import annotations

import argparse
import csv
import email.utils
import html as html_lib
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

import update_compute_grid_daily as daily


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HTML = ROOT / "compute-grid" / "index.html"
if not DEFAULT_HTML.exists():
    DEFAULT_HTML = ROOT / "index.html"
DATA_DIR = DEFAULT_HTML.parent / "data"
CHECKPOINT = DATA_DIR / "refresh_checkpoint.json"
PROGRESS = DATA_DIR / "refresh_progress.log"
LATEST_JSON = DATA_DIR / "latest.json"
LATEST_CSV = DATA_DIR / "latest_market_rows.csv"

NEWS_TOPICS = {
    "Power / Grid": "AI data center power grid interconnection transformers switchgear latest",
    "Memory / Packaging": "AI HBM advanced packaging semiconductor substrate latest",
    "Networking / Interconnect": "AI data center networking 800G optics latest",
    "Cooling / Thermal": "AI data center liquid cooling rack density latest",
    "Hyperscaler Capex": "AI data center capex Microsoft Amazon Meta Google latest",
}


@dataclass
class Progress:
    total: int = 0
    completed: int = 0
    failed: int = 0
    current_item: str | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{utc_now()}] {message}"
    with PROGRESS.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line)


def write_checkpoint(status: str, step: str, progress: Progress, extra: dict | None = None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "task": "compute_grid_project_refresh",
        "status": status,
        "current_step": step,
        "total": progress.total,
        "completed": progress.completed,
        "failed": progress.failed,
        "current_item": progress.current_item,
        "last_update": utc_now(),
    }
    if extra:
        payload.update(extra)
    CHECKPOINT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def parse_js_array(raw: str) -> list[str]:
    try:
        return json.loads("[" + raw + "]")
    except json.JSONDecodeError:
        return []


def extract_ytd_labels(page_html: str) -> list[str]:
    return sorted(daily.extract_ytd_map(page_html).keys())


def extract_categories(page_html: str) -> dict[str, list[str]]:
    categories: dict[str, list[str]] = {}
    pattern = re.compile(r'\{\s*title:\s*"([^"]+)"[\s\S]*?tickers:\s*\[(.*?)\]', re.M)
    for title, raw_tickers in pattern.findall(page_html):
        tickers = [str(x) for x in parse_js_array(raw_tickers)]
        if tickers:
            categories[title] = tickers
    return categories


def symbol_for_label(label: str) -> str | None:
    return daily.label_to_symbol(label)


def pct_change(first: float | None, last: float | None) -> float | None:
    if first is None or last is None or first <= 0:
        return None
    return round((last / first - 1) * 100, 1)


def fetch_market_row(label: str, symbol: str, year: int) -> dict:
    try:
        import yfinance as yf  # type: ignore

        hist = yf.Ticker(symbol).history(period="6mo", auto_adjust=False, actions=False)
        if hist is None or hist.empty:
            return {"label": label, "symbol": symbol, "status": "no_data"}
        series = hist["Adj Close"] if "Adj Close" in hist.columns else hist["Close"]
        series = series.dropna()
        if len(series) < 2:
            return {"label": label, "symbol": symbol, "status": "too_few_rows"}
        latest = float(series.iloc[-1])
        latest_date = str(series.index[-1].date())
        ytd = daily.fetch_ytd(label, symbol, year)
        row = {
            "label": label,
            "symbol": symbol,
            "status": "ok",
            "latest_date": latest_date,
            "latest_close": round(latest, 4),
            "ytd_pct": ytd,
            "return_5d_pct": pct_change(float(series.iloc[-6]), latest) if len(series) >= 6 else None,
            "return_20d_pct": pct_change(float(series.iloc[-21]), latest) if len(series) >= 21 else None,
            "return_60d_pct": pct_change(float(series.iloc[-61]), latest) if len(series) >= 61 else None,
        }
        return row
    except Exception as exc:
        return {"label": label, "symbol": symbol, "status": "failed", "error": repr(exc)}


def parse_rss_datetime(value: str) -> str | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return None


def fetch_news_topic(topic: str, query: str, limit: int = 4) -> dict:
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query + " when:7d", "hl": "en-US", "gl": "US", "ceid": "US:en"}
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            root = ET.fromstring(resp.read())
        items = []
        for item in root.findall("./channel/item")[:limit]:
            source = item.find("source")
            items.append(
                {
                    "title": html_lib.unescape(item.findtext("title") or "").strip(),
                    "url": item.findtext("link") or "",
                    "source": source.text if source is not None else "Google News",
                    "published_at": parse_rss_datetime(item.findtext("pubDate") or "") or item.findtext("pubDate"),
                }
            )
        return {"topic": topic, "query": query, "ok": True, "url": url, "items": items}
    except Exception as exc:
        return {"topic": topic, "query": query, "ok": False, "url": url, "error": repr(exc), "items": []}


def clean_number(value) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return None
        return out
    except Exception:
        return None


def summarize_categories(categories: dict[str, list[str]], market_rows: dict[str, dict]) -> list[dict]:
    summaries = []
    for title, labels in categories.items():
        rows = [market_rows.get(label) for label in labels]
        ok_rows = [r for r in rows if r and r.get("status") == "ok"]
        ytd_values = [clean_number(r.get("ytd_pct")) for r in ok_rows]
        r20_values = [clean_number(r.get("return_20d_pct")) for r in ok_rows]
        ytd_values = [v for v in ytd_values if v is not None]
        r20_values = [v for v in r20_values if v is not None]
        top = sorted(ok_rows, key=lambda r: clean_number(r.get("return_20d_pct")) or -999, reverse=True)[:3]
        summaries.append(
            {
                "category": title,
                "tickers": len(labels),
                "covered": len(ok_rows),
                "avg_ytd_pct": round(mean(ytd_values), 1) if ytd_values else None,
                "avg_20d_pct": round(mean(r20_values), 1) if r20_values else None,
                "positive_ytd_share": round(sum(1 for v in ytd_values if v > 0) / len(ytd_values), 2) if ytd_values else None,
                "top_20d": [r["label"] for r in top],
            }
        )
    summaries.sort(key=lambda r: clean_number(r.get("avg_20d_pct")) or -999, reverse=True)
    return summaries


def replace_one(page_html: str, pattern: str, replacement: str, label: str, flags: int = 0) -> str:
    new_html, count = re.subn(pattern, replacement, page_html, count=1, flags=flags)
    if count != 1:
        raise RuntimeError(f"Could not replace {label}")
    return new_html


def replace_project_field(page_html: str, key: str, value: str) -> str:
    return replace_one(
        page_html,
        rf'(<b data-project="{re.escape(key)}">)(.*?)(</b>)',
        lambda match: f"{match.group(1)}{value}{match.group(3)}",
        f"project field {key}",
        re.S,
    )


def update_page(page_html: str, report: dict, skip_ytd: bool) -> str:
    zurich = datetime.now(ZoneInfo("Europe/Zurich"))
    display_date = zurich.strftime("%d %b %Y")
    category_summaries = report["category_summaries"]
    strongest = category_summaries[0] if category_summaries else {}
    weakest = min(
        category_summaries,
        key=lambda r: clean_number(r.get("avg_20d_pct")) if clean_number(r.get("avg_20d_pct")) is not None else 999,
    ) if category_summaries else {}
    ok_rows = [r for r in report["market_rows"] if r.get("status") == "ok"]
    stale_rows = [r for r in report["market_rows"] if r.get("status") != "ok"]
    news_items = sum(len(topic.get("items") or []) for topic in report["news_topics"])

    pressure = "High / rising" if (clean_number(strongest.get("avg_20d_pct")) or 0) > 5 else "High / stable"
    change = (
        f"{strongest.get('category', 'Compute Grid')} leads the latest scan; "
        f"{weakest.get('category', 'watch weaker layers')} is the softest measured layer."
    )
    market = (
        f"<strong>{len(ok_rows)}</strong> public tickers measured; "
        f"<strong>{news_items}</strong> fresh theme headlines collected."
    )

    page_html = daily.replace_pulse(page_html, "date", display_date)
    page_html = daily.replace_pulse(page_html, "pressure", pressure)
    page_html = daily.replace_pulse(page_html, "change", html_lib.escape(change))
    page_html = daily.replace_pulse(page_html, "market", market)
    page_html = replace_one(page_html, r'<span class="chip">Updated [^<]+</span>', f'<span class="chip">Updated {display_date}</span>', "updated chip")

    if not skip_ytd:
        ytd_values = {
            row["label"]: row.get("ytd_pct")
            for row in report["market_rows"]
            if row.get("label") in report["labels"]
        }
        for label in report["labels"]:
            ytd_values.setdefault(label, None)
        page_html = re.sub(r"const ytdMap = \{.*?\n\s*\};", daily.format_js_map(ytd_values), page_html, count=1, flags=re.S)

    if 'data-project="freshness"' in page_html:
        page_html = replace_project_field(page_html, "freshness", display_date)
        page_html = replace_project_field(page_html, "universe", f"{len(ok_rows)} / {len(report['labels'])}")
        page_html = replace_project_field(page_html, "strongest", str(strongest.get("category") or "-"))
        page_html = replace_project_field(page_html, "softest", str(weakest.get("category") or "-"))
        page_html = replace_project_field(page_html, "headlines", str(news_items))
        page_html = replace_project_field(page_html, "limits", f"{len(stale_rows)} ticker gaps; public prices/RSS only")

    return page_html


def write_csv(rows: list[dict]) -> None:
    fields = ["label", "symbol", "status", "latest_date", "latest_close", "ytd_pct", "return_5d_pct", "return_20d_pct", "return_60d_pct", "error"]
    with LATEST_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})


def run(args: argparse.Namespace) -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROGRESS.write_text("", encoding="utf-8")
    progress = Progress()
    write_checkpoint("running", "load_page", progress)
    page_html = args.html.read_text(encoding="utf-8")
    labels = extract_ytd_labels(page_html)
    categories = extract_categories(page_html)
    log(f"loaded page labels={len(labels)} categories={len(categories)}")

    if args.preflight:
        labels = labels[: min(args.limit, len(labels))]
    elif args.limit:
        labels = labels[: args.limit]
    progress.total = len(labels) + len(NEWS_TOPICS)
    write_checkpoint("running", "market_data", progress, {"labels": len(labels), "categories": len(categories)})

    year = datetime.now(ZoneInfo("Europe/Zurich")).year
    market_rows: list[dict] = []
    market_by_label: dict[str, dict] = {}
    for label in labels:
        progress.current_item = label
        symbol = symbol_for_label(label)
        if symbol is None:
            row = {"label": label, "symbol": None, "status": "private_or_unlisted"}
        else:
            row = fetch_market_row(label, symbol, year)
        market_rows.append(row)
        market_by_label[label] = row
        if row.get("status") == "ok" or row.get("status") == "private_or_unlisted":
            progress.completed += 1
        else:
            progress.failed += 1
        if args.pause:
            time.sleep(args.pause)
        if (progress.completed + progress.failed) % 10 == 0:
            log(f"market progress {progress.completed + progress.failed}/{progress.total} failed={progress.failed}")
            write_checkpoint("running", "market_data", progress)

    log("collecting news topics")
    news_topics = []
    for topic, query in NEWS_TOPICS.items():
        progress.current_item = topic
        news = fetch_news_topic(topic, query)
        news_topics.append(news)
        if news.get("ok"):
            progress.completed += 1
        else:
            progress.failed += 1
        write_checkpoint("running", "news", progress)
        time.sleep(0.2)

    category_summaries = summarize_categories(categories, market_by_label)
    report = {
        "task": "compute_grid_project_refresh",
        "generated_at": utc_now(),
        "page": str(args.html),
        "labels": labels,
        "categories": categories,
        "market_rows": market_rows,
        "category_summaries": category_summaries,
        "news_topics": news_topics,
        "sources": [
            "Yahoo Finance via yfinance",
            "Google News RSS",
            "Existing public Compute Grid source links",
        ],
        "limitations": [
            "Market data is public delayed/official-close data, not live trading advice.",
            "Google News RSS headlines are used as collection signals, not verified full-text research.",
            "Private or unmapped suppliers cannot receive market metrics.",
        ],
    }

    write_checkpoint("running", "write_artifacts", progress)
    LATEST_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    write_csv(market_rows)
    updated_html = update_page(page_html, report, skip_ytd=args.skip_ytd)
    if not args.no_write_html:
        args.html.write_text(updated_html, encoding="utf-8")
    progress.current_item = None
    write_checkpoint("completed", "done", progress, {"json": str(LATEST_JSON), "csv": str(LATEST_CSV)})
    log(f"completed ok={progress.completed} failed={progress.failed} json={LATEST_JSON}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh Compute Grid project data and page.")
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML)
    parser.add_argument("--preflight", action="store_true", help="Run a tiny end-to-end sample.")
    parser.add_argument("--limit", type=int, default=0, help="Limit ticker labels for smoke/preflight.")
    parser.add_argument("--skip-ytd", action="store_true", help="Do not replace ytdMap values.")
    parser.add_argument("--no-write-html", action="store_true")
    parser.add_argument("--pause", type=float, default=0.02)
    args = parser.parse_args()
    try:
        return run(args)
    except Exception as exc:
        progress = Progress()
        write_checkpoint("failed", "exception", progress, {"error": repr(exc)})
        log(f"FAILED {exc!r}")
        raise


if __name__ == "__main__":
    sys.exit(main())
