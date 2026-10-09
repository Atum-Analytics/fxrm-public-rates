# fxrm-public-rates

Public rate feed for **FXRM Treasury** (`https://fxrm-ai-dashboard.azurewebsites.net/`).

Two data feeds, both auto-updated by scheduled GitHub Actions workflows:

| File | Source | Workflow | Consumed by |
|---|---|---|---|
| `public-rates/latest.json` | FBIL → RBI → XE cascade | `scrape-rbi-rates.yml` (every 15 min, 03:30-13:00 UTC Mon-Fri) | `fetchGithubRelayRates()` in FXRM `server.js` |
| `public-rates/sbi-latest.json` | SBI Forex Card Rates PDF | `scrape-sbi-card-rates.yml` (every 30 min, 04:00-13:00 UTC Mon-Fri) | `fetchSbiCardRates()` in FXRM `server.js` |

## SBI Rate Schema

```jsonc
{
  "scrapedAt": "2026-10-09T05:18:00Z",
  "sourceUrl": "https://sbi.bank.in/documents/16012/1400784/FOREX_CARD_RATES.pdf",
  "sbiPublishedDate": "2026-10-09",
  "sbiPublishedTime": "10:48 AM",
  "currencyCount": 30,
  "currencies": {
    "USD": {
      "name": "UNITED STATES DOLLAR",
      "tt_buy":  96.20, "tt_sell":  97.05,
      "bill_buy": 96.13, "bill_sell": 97.22,
      "card_buy": 0,     "card_sell": 0,
      "cn_buy":  95.00, "cn_sell":  97.60,
      "quoted_per_unit": 1
    },
    "JPY": { ..., "quoted_per_unit": 100 }
  }
}
```

`tt_*` = Telegraphic Transfer (wire). This is the rate an importer gets when booking
a forward through SBI's corporate FX desk. JPY/THB/KRW are published per 100
units in SBI's PDF — we normalise to per-1 unit in this JSON.

## Local test

```bash
pip install pdfplumber
python scripts/scrape_sbi_card_rates.py
cat public-rates/sbi-latest.json
```

## Workflow activation

Both scheduled workflow YAMLs are kept under `.github-staging/workflows/` because
the OAuth token used to push lacks the `workflow` scope. To activate: move the
file into `.github/workflows/` via the GitHub web UI (one commit, no local push).
