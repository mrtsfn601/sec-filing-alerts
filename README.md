# sec-filing-alerts

Always-on watcher that polls **SEC EDGAR** for new filings by a watchlist of
entities and pushes a **Telegram** alert. For **13F-HR** filings the alert
includes a deterministic holdings table + a diff vs the prior quarter
(parsed in Python — no LLM, no API cost, no AI-summary errors).

Runs on **GitHub Actions cron** (every 5 min). Free on a public repo.
Contains **no secrets and no private data** — only public CIKs and public
filing accession numbers.

## How it works

1. `watch.py` reads `watchlist.json` and `state.json`.
2. For each entity it pulls `https://data.sec.gov/submissions/CIK##########.json`.
3. Any filing whose form matches and whose accession isn't in `state.json` is **new**.
4. New 13F → fetch the information-table XML, aggregate by issuer + put/call,
   diff vs the most recent prior 13F. Other forms → form + headline + EDGAR link.
5. Send to Telegram; record the accession in `state.json` (committed back on change).

## Add an entity (generalization)

Append to `watchlist.json`:

```json
{ "name": "Berkshire Hathaway", "cik": "0001067983", "forms": ["13F-HR", "13F-HR/A"] }
```

- `forms`: list of exact EDGAR form strings (e.g. `"13F-HR"`, `"SCHEDULE 13G"`, `"4"`),
  or `["*"]` to alert on **every** form.
- Don't know the CIK? `python resolve_cik.py "Berkshire Hathaway"` (or a ticker).

## Secrets (set in GitHub → Settings → Secrets and variables → Actions)

| Secret | What |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From @BotFather. |
| `TELEGRAM_CHAT_ID`   | Your chat ID — message the bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `result[].message.chat.id`. |
| `STATE_PUSH_TOKEN`   | Fine-grained PAT with **contents: read/write** on this repo (lets the job commit `state.json` and keeps the schedule alive). |

Never commit these. The code reads them from the environment only.

## Manual runs

Actions tab → **watch-edgar** → *Run workflow* → `mode`:
- `test` — send a one-off Telegram test message.
- `seed` — mark all *current* filings as seen (no alerts). Run once at setup.
- `dry-run` — detect + print, send nothing.

Local: `python watch.py [--test|--seed|--dry-run]`.

## Notes / limits

- SEC fair-access: a descriptive `User-Agent` is sent; polling stays well under 10 req/s.
- GitHub `schedule` can lag 5–30 min under load and pauses after 60 days of
  inactivity — `keepalive.yml` mitigates the latter. For strict minute-level
  reliability, run `watch.py` from system cron on an always-on box instead.
- 13F `value` is reported in dollars (post-2023 EDGAR); put/call is read straight
  from the filing's `putCall` field.
