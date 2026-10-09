#!/usr/bin/env python3
"""Scrape SBI Forex Card Rates PDF and write a structured JSON feed.

Source:
  https://sbi.bank.in/documents/16012/1400784/FOREX_CARD_RATES.pdf

The PDF holds a single 1-page table with 30 currencies × 8 rate columns:
  TT_BUY  TT_SELL  BILL_BUY  BILL_SELL  CARD_BUY  CARD_SELL  CN_BUY  CN_SELL

TT (Telegraphic Transfer) = the rate corporates use for wire transfers —
this is the actual deal-execution rate for an importer booking a forward
through SBI. BILL = used for bill of exchange / draft settlement.
CARD = pre-paid travel card. CN = currency notes (cash).

Writes:
  public-rates/sbi-latest.json       (newest snapshot)
  public-rates/history/sbi_<yyyymmdd_hhmm>.json

Schema:
  {
    "scrapedAt": "2026-10-09T05:18:00Z",
    "sbiPublishedDate": "2026-10-09",
    "sbiPublishedTime": "10:48 AM",
    "sourceUrl": "...",
    "currencies": {
      "USD": { "tt_buy": 96.20, "tt_sell": 97.05, "bill_buy": 96.13,
               "bill_sell": 97.22, "card_buy": 0, "card_sell": 0,
               "cn_buy": 95.00, "cn_sell": 97.60 },
      "EUR": { ... }, ...
    }
  }

NOTE: JPY/THB/KRW are published per 100 units — we normalise to per-1 unit
in the JSON so downstream math doesn't need to special-case.
"""
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    print("ERROR: pdfplumber not installed. Add it to requirements.txt or workflow setup.", file=sys.stderr)
    sys.exit(1)

SBI_URL = "https://sbi.bank.in/documents/16012/1400784/FOREX_CARD_RATES.pdf"
UA = "Mozilla/5.0 (compatible; FXRM-RateBot/2.3; +https://fxrm-ai-dashboard.azurewebsites.net)"
PDF_PATH = Path("/tmp/sbi_forex_card_rates.pdf")

# Currencies published per 100 units in the SBI PDF — the quoted rate is for
# 100 units, so divide by 100 to get a per-unit rate.
PER_100_CODES = {"JPY", "THB", "KRW", "VND", "IDR"}


def fetch_pdf() -> Path:
    req = urllib.request.Request(SBI_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        PDF_PATH.parent.mkdir(parents=True, exist_ok=True)
        PDF_PATH.write_bytes(r.read())
    return PDF_PATH


def parse_pdf(path: Path) -> dict:
    """Extract date, time, and the full currency table from the PDF."""
    with pdfplumber.open(str(path)) as pdf:
        text = pdf.pages[0].extract_text()

    # Date / Time
    m_date = re.search(r"Date\s*(\d{2}-\d{2}-\d{4})", text)
    m_time = re.search(r"Time\s*(\d{1,2}:\d{2}\s*(?:AM|PM))", text, re.IGNORECASE)
    date_iso = None
    if m_date:
        try:
            date_iso = datetime.strptime(m_date.group(1), "%d-%m-%Y").date().isoformat()
        except ValueError:
            pass

    # Currency rows — pattern:
    #   <name words> CCY/INR  t1 t2 t3 t4 t5 t6 t7 t8
    # Example: UNITED STATES DOLLAR USD/INR 96.2 97.05 96.13 97.22 96.13 97.22 95 97.6
    row_re = re.compile(
        r"([A-Z][A-Z\s&().'-]+?)\s+([A-Z]{3})/INR\s+"
        r"(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+"
        r"(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+"
        r"(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+"
        r"(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)"
    )
    currencies = {}
    for m in row_re.finditer(text):
        # Strip any header lines captured by the multiline regex — keep only
        # the final line's words as the currency's display name.
        name_raw = m.group(1).strip()
        name = re.sub(r".*\n", "", name_raw).strip()
        code = m.group(2).upper()
        vals = [float(m.group(i)) for i in range(3, 11)]
        tt_buy, tt_sell, bill_buy, bill_sell, card_buy, card_sell, cn_buy, cn_sell = vals
        if code in PER_100_CODES:
            # SBI publishes JPY/THB/KRW per 100 — convert to per-1 rate
            tt_buy, tt_sell = tt_buy / 100, tt_sell / 100
            bill_buy, bill_sell = bill_buy / 100, bill_sell / 100
            card_buy, card_sell = card_buy / 100, card_sell / 100
            cn_buy, cn_sell = cn_buy / 100, cn_sell / 100
        currencies[code] = {
            "name": name,
            "tt_buy": round(tt_buy, 4),
            "tt_sell": round(tt_sell, 4),
            "bill_buy": round(bill_buy, 4),
            "bill_sell": round(bill_sell, 4),
            "card_buy": round(card_buy, 4),
            "card_sell": round(card_sell, 4),
            "cn_buy": round(cn_buy, 4),
            "cn_sell": round(cn_sell, 4),
            "quoted_per_unit": 100 if code in PER_100_CODES else 1,
        }
    return {"date": date_iso, "time": m_time.group(1) if m_time else None, "currencies": currencies}


def main() -> int:
    try:
        fetch_pdf()
    except Exception as e:
        print(f"ERROR fetching PDF: {e}", file=sys.stderr)
        return 1

    try:
        parsed = parse_pdf(PDF_PATH)
    except Exception as e:
        print(f"ERROR parsing PDF: {e}", file=sys.stderr)
        return 1

    if not parsed["currencies"]:
        print("ERROR: no currencies parsed from PDF — layout may have changed", file=sys.stderr)
        return 1

    now_utc = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    payload = {
        "scrapedAt": now_utc,
        "sourceUrl": SBI_URL,
        "sbiPublishedDate": parsed["date"],
        "sbiPublishedTime": parsed["time"],
        "currencyCount": len(parsed["currencies"]),
        "currencies": parsed["currencies"],
    }

    out_dir = Path("public-rates")
    out_dir.mkdir(exist_ok=True)
    (out_dir / "history").mkdir(exist_ok=True)

    (out_dir / "sbi-latest.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    (out_dir / "history" / f"sbi_{stamp}.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )

    usd = parsed["currencies"].get("USD", {})
    print(f"OK  SBI published {parsed['date']} {parsed['time']}  "
          f"USD TT buy/sell = {usd.get('tt_buy')}/{usd.get('tt_sell')}  "
          f"{len(parsed['currencies'])} currencies")
    return 0


if __name__ == "__main__":
    sys.exit(main())
