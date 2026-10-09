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
import urllib.parse

SBI_URL = "https://sbi.bank.in/documents/16012/1400784/FOREX_CARD_RATES.pdf"
# Known SBI PDF locations tried in order. Added as SBI restructures their site
# every 1-2 years — recent change: sbi.co.in -> sbi.bank.in.
SBI_FALLBACK_URLS = [
    "https://sbi.bank.in/documents/16012/1400784/FOREX_CARD_RATES.pdf",
    "https://sbi.co.in/documents/16012/1400784/FOREX_CARD_RATES.pdf",
    "https://bank.sbi/documents/16012/1400784/FOREX_CARD_RATES.pdf",
    "https://sbi.bank.in/documents/16012/1400784/forex.PDF",
    "https://sbi.co.in/web/business/forex-services/forex-card-rates",
]
UA = "Mozilla/5.0 (compatible; FXRM-RateBot/2.3; +https://fxrm-ai-dashboard.azurewebsites.net)"
PDF_PATH = Path("/tmp/sbi_forex_card_rates.pdf")


def _fetch_html(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept": "text/html",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", errors="replace")


def search_sbi_pdf_url(query: str = "STATE BANK OF INDIA FOREX CARD RATES") -> list[str]:
    """Last-resort fallback: when all hardcoded URLs 404, search the open web
    for the current SBI forex-card-rate PDF location.

    Tries DuckDuckGo first, then Bing. Both return HTML pages where SBI PDF
    URLs appear either directly or wrapped in redirect links.
    Returns up to 5 PDF candidates ranked by path specificity.
    """
    candidates: list[str] = []

    for engine_name, engine_url_tpl in (
        ("duckduckgo", "https://duckduckgo.com/html/?q={}"),
        ("bing",       "https://www.bing.com/search?q={}"),
    ):
        try:
            url = engine_url_tpl.format(urllib.parse.quote(query))
            html = _fetch_html(url)
        except Exception as e:
            print(f"[search:{engine_name}] failed: {e}", file=sys.stderr)
            continue

        # (a) Direct SBI/bank.sbi/sbi.co.in URLs embedded in result markup
        for m in re.finditer(
            r'https?://(?:sbi\.bank\.in|sbi\.co\.in|bank\.sbi)/[^\s"\'<>]+?\.(?:pdf|PDF)',
            html,
        ):
            u = m.group(0)
            if u not in candidates:
                candidates.append(u)

        # (b) DuckDuckGo redirect wrapper: /l/?uddg=<url-encoded-target>
        for m in re.finditer(r'/l/\?uddg=([^"&]+)', html):
            try:
                target = urllib.parse.unquote(m.group(1))
                if re.match(r"^https?://(sbi\.bank\.in|sbi\.co\.in|bank\.sbi)", target):
                    if target.lower().endswith(".pdf") and target not in candidates:
                        candidates.append(target)
            except Exception:
                pass

        if candidates:
            print(f"[search:{engine_name}] found {len(candidates)} candidate(s)", file=sys.stderr)
            break  # don't hit the next engine if we already have something

    # Rank: prefer URLs with 'forex' + 'card' + 'rate' in the path (most specific)
    def score(u: str) -> int:
        s = u.lower()
        return (
            (2 if "forex" in s else 0)
            + (2 if "card" in s else 0)
            + (1 if "rate" in s else 0)
            + (1 if "sbi.bank.in" in s else 0)  # most recent domain
        )

    candidates.sort(key=score, reverse=True)
    return candidates[:5]

# Currencies published per 100 units in the SBI PDF — the quoted rate is for
# 100 units, so divide by 100 to get a per-unit rate.
PER_100_CODES = {"JPY", "THB", "KRW", "VND", "IDR"}


def fetch_pdf() -> tuple[Path, str]:
    """Try known SBI PDF locations in order, then fall back to internet search
    when the hardcoded list fails. Returns (local_path, source_url) when any
    attempt yields a valid PDF ≥ 10 KB.
    """
    PDF_PATH.parent.mkdir(parents=True, exist_ok=True)

    def _try(url: str) -> bool:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                body = r.read()
            if len(body) < 10_000:
                return False
            # Verify PDF magic header
            if not body[:4] == b"%PDF":
                return False
            PDF_PATH.write_bytes(body)
            return True
        except Exception:
            return False

    # 1. Hardcoded candidates (most reliable path on happy days)
    for url in SBI_FALLBACK_URLS:
        print(f"[fetch] try {url}", file=sys.stderr)
        if _try(url):
            print(f"[fetch] OK  {url}  ({PDF_PATH.stat().st_size:,} bytes)", file=sys.stderr)
            return PDF_PATH, url

    # 2. Last resort: search the open web for the current PDF location
    print("[fetch] all hardcoded URLs failed; searching DuckDuckGo…", file=sys.stderr)
    candidates = search_sbi_pdf_url()
    print(f"[fetch] search found {len(candidates)} candidate(s): {candidates}", file=sys.stderr)
    for url in candidates:
        if _try(url):
            print(f"[fetch] OK via search: {url}", file=sys.stderr)
            return PDF_PATH, url

    raise RuntimeError("Could not locate SBI Forex Card Rates PDF via any known URL or search fallback.")


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
        _, resolved_url = fetch_pdf()
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
        "sourceUrl": resolved_url,
        "sourceUrlDiscoveredByFallback": resolved_url != SBI_URL,
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
