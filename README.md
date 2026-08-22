# sec-filing-alerts

Always-on watcher that polls **SEC EDGAR** for new filings by a watchlist of
entities and pushes a **Telegram** alert. For **13F-HR** filings the alert
includes a deterministic holdings table + a diff vs the prior quarter
(parsed in Python — no LLM, no API cost, no AI-summary errors).

Runs on **GitHub Actions cron** (every 5 min). Free on a public repo.
Contains **no secrets and no private data** — only public CIKs and public
filing accession numbers.

## How it works

1. `watchers/edgar.py` reads `config/edgar.json` and `state/edgar.json`.
2. For each entity it pulls `https://data.sec.gov/submissions/CIK##########.json`.
3. Any filing whose form matches and whose accession isn't in `state/edgar.json` is **new**.
4. New 13F → fetch the information-table XML, aggregate by issuer + put/call,
   diff vs the most recent prior 13F. Other forms → form + headline + EDGAR link.
5. Send to Telegram; record the accession in `state/edgar.json` (committed back on change).

## Add an entity (generalization)

Append to `config/edgar.json`:

```json
{ "name": "Berkshire Hathaway", "cik": "0001067983", "forms": ["13F-HR", "13F-HR/A"] }
```

- `forms`: list of EDGAR form types (e.g. `"13F"`, `"SCHEDULE 13G"`, `"4"`), or
  `["*"]` to alert on **every** form. Matching is **by token, not substring**:
  `"4"` matches `4` and `4/A` but *not* `S-4`, `424B3`, `DEF 14A` or `144`;
  `"13D"` matches `13D`, `SC 13D/A` and `SCHEDULE 13D`; `"13F"` matches
  `13F-HR`, `13F-NT` and `13FCONP`. This matters most for operating-company
  CIKs, whose feeds are dominated by forms an insider filter should ignore.
- Don't know the CIK? `python tools/resolve_cik.py "Berkshire Hathaway"` (or a ticker).

## Secrets (set in GitHub → Settings → Secrets and variables → Actions)

| Secret | What |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From @BotFather. |
| `TELEGRAM_CHAT_ID`   | Your chat ID — message the bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `result[].message.chat.id`. |
| `STATE_PUSH_TOKEN`   | Fine-grained PAT with **contents: read/write** on this repo (lets the job commit `state/*.json` and keeps the schedule alive). |

Never commit these. The code reads them from the environment only.

## Manual runs

Actions tab → **watch-edgar** → *Run workflow* → `mode`:
- `test` — send a one-off Telegram test message.
- `seed` — mark all *current* filings as seen (no alerts). Run once at setup.
- `dry-run` — detect + print, send nothing.

Local, from the repo root: `python -m watchers.edgar [--test|--seed|--dry-run]`
(likewise `watchers.senate`, `watchers.house`, `watchers.oge`).

## Tests

No framework or dependencies — plain asserts, run them directly:

```bash
python3 tests/test_form_matches.py        # watchlist `forms` matching
python3 tests/test_state_persistence.py   # alerts survive a failed Telegram send
```

## Layout

```
common/     shared: notify (Telegram), http, store (config+state paths, JSON),
            fmt (display), pdf (pdftotext)
watchers/   one module per source: edgar, senate, house, oge
config/     watchlists — edgar.json, senate.json, house.json, oge.json
state/      seen-markers, committed back by CI — same four names
tests/      plain asserts, no framework
tools/      resolve_cik.py
```

Watchers are run as modules (`python -m watchers.edgar`) so `common/` resolves;
running `python watchers/edgar.py` directly will not work.

## Notes / limits

- SEC fair-access: a descriptive `User-Agent` is sent; polling stays well under 10 req/s.
- GitHub `schedule` can lag 5–30 min under load and pauses after 60 days of
  inactivity — `keepalive.yml` mitigates the latter. For strict minute-level
  reliability, run the watchers from system cron on an always-on box instead.
- 13F `value` is reported in dollars (post-2023 EDGAR); put/call is read straight
  from the filing's `putCall` field.
