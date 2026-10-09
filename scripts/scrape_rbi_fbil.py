#!/usr/bin/env python3
"""Scrape RBI/FBIL daily reference rates from a non-Azure IP (GitHub Actions runner).

Order of attempts (first-usable wins):
  1. FBIL — https://www.fbil.org.in/FbilSetDailyRate      (JSON blob)
  2. FBIL — https://fbil.org.in/api/forex/getReferenceRates
  3. RBI  — https://www.rbi.org.in/scripts/ReferenceRateArchive.aspx (HTML table scrape)
  4. XE   — https://www.xe.com/currencyconverter/convert/?Amount=1&From=USD&To=INR
             (fallback; mid-market rate — not an official reference)

Writes:
  public-rates/latest.json         — current snapshot
  public-rates/history/<yyyymmdd>_<hhmm>.json  — append-only hourly archive

Schema of latest.json:
  {
    "scrapedAt": "2026-10-09T05:33:00Z",
    "asOf":      "2026-10-09T05:33:00Z",
    "scrapedFrom": "FBIL" | "RBI" | "XE",
    "rates": { "USD": 96.4486, "EUR": 108.37, "GBP": 128.42, "JPY": 0.6512 }
  }
"""
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

UA = "Mozilla/5.0 (compatible; FXRM-RateBot/1.0; +https://fxrm-ai-dashboard.azurewebsites.net)"
TIMEOUT = 12


def fetch(url: str, timeout: int = TIMEOUT) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/html;q=0.9, */*;q=0.5",
        "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def try_fbil() -> dict | None:
    """FBIL has two historical URLs; try both."""
    for url in (
        "https://www.fbil.org.in/FbilSetDailyRate",
        "https://fbil.org.in/api/forex/getReferenceRates",
    ):
        try:
            raw = fetch(url)
            # FBIL mixes HTML and JSON — pull the JSON blob out if needed
            txt = raw.strip()
            m = re.search(r'\{[^{}]*"USD"[^{}]*\}', txt)
            if m:
                txt = m.group(0)
            try:
                data = json.loads(txt)
            except Exception:
                continue
            rates = {}
            # Shape A: { "USD": 96.4486, "EUR": 108.37, ... }
            # Shape B: { "rates": [{"currency": "USD", "rate": 96.4486}, ...] }
            if isinstance(data, dict):
                if isinstance(data.get("rates"), list):
                    for r in data["rates"]:
                        c = str(r.get("currency") or r.get("ccy") or "").upper()
                        v = float(r.get("rate") or r.get("value") or 0)
                        if c and v:
                            rates[c] = round(v, 4)
                else:
                    for c in ("USD", "EUR", "GBP", "JPY"):
                        v = data.get(c) or data.get(c.lower())
                        if v:
                            try:
                                rates[c] = round(float(v), 4)
                            except Exception:
                                pass
            if rates.get("USD"):
                return {"source": "FBIL", "rates": rates}
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, Exception) as e:
            print(f"[fbil] {url} failed: {e}", file=sys.stderr)
            continue
    return None


def try_rbi() -> dict | None:
    """RBI reference-rate archive HTML scrape."""
    url = "https://www.rbi.org.in/Scripts/BS_ViewDownloadRefRateHistory.aspx"
    try:
        html = fetch(url)
        rates = {}
        for ccy in ("USD", "EUR", "GBP", "JPY"):
            m = re.search(rf"{ccy}\s*</td>\s*<td[^>]*>\s*([0-9]+\.[0-9]+)", html, re.IGNORECASE)
            if m:
                rates[ccy] = round(float(m.group(1)), 4)
        if rates.get("USD"):
            return {"source": "RBI", "rates": rates}
    except Exception as e:
        print(f"[rbi] failed: {e}", file=sys.stderr)
    return None


def try_xe() -> dict | None:
    """XE mid-market as last-resort fallback. Not an official reference."""
    try:
        html = fetch("https://www.xe.com/currencyconverter/convert/?Amount=1&From=USD&To=INR")
        m = re.search(r"(\d{2,3}\.\d{3,6})\s*Indian\s*Rupees?", html)
        if not m:
            return None
        return {"source": "XE", "rates": {"USD": round(float(m.group(1)), 4)}}
    except Exception as e:
        print(f"[xe] failed: {e}", file=sys.stderr)
        return None


def main() -> int:
    scraped = None
    for probe in (try_fbil, try_rbi, try_xe):
        scraped = probe()
        if scraped:
            break
    if not scraped:
        print("ERROR: no source yielded a usable rate", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    payload = {
        "scrapedAt": now,
        "asOf": now,
        "scrapedFrom": scraped["source"],
        "rates": scraped["rates"],
    }

    out_dir = Path("public-rates")
    out_dir.mkdir(exist_ok=True)
    hist_dir = out_dir / "history"
    hist_dir.mkdir(exist_ok=True)

    # Latest
    (out_dir / "latest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # Hourly archive (dedup by hour)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H00")
    (hist_dir / f"{stamp}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"OK  source={scraped['source']}  rates={scraped['rates']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
