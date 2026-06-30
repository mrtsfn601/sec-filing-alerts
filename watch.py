#!/usr/bin/env python3
"""
sec-filing-alerts — poll SEC EDGAR for new filings by watched entities and
push a Telegram alert. For 13F-HR filings the alert groups holdings into
Stocks / Calls / Puts (with subtotals) and diffs vs the prior quarter —
all parsed deterministically in Python (no LLM).

Generalizable: add any entity to watchlist.json as {name, cik, forms}.
  forms: ["*"]            -> alert on every form
  forms: ["13F-HR", ...]  -> only the listed form types (exact EDGAR strings)

Usage:
  python watch.py            # normal: detect new filings, alert, update state
  python watch.py --seed     # mark all current filings as seen, send nothing
  python watch.py --test     # send a one-off test message
  python watch.py --dry-run  # detect + print to stdout, send nothing, save nothing

Env (from GitHub Actions secrets; never hard-coded):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST = os.path.join(HERE, "watchlist.json")
STATE = os.path.join(HERE, "state.json")

# SEC fair-access policy requires a descriptive User-Agent (NOT a secret).
# Note: SEC's WAF rejects UAs containing a URL (e.g. "github.com/..").
UA = "sec-filing-alerts mrtsfn601 maratsafin601@gmail.com"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"
ARCHIVE_DIR = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/"
INDEX_JSON = ARCHIVE_DIR + "index.json"
FILING_INDEX = ARCHIVE_DIR + "{accession}-index.html"

TG_API = "https://api.telegram.org/bot{token}/{method}"
TG_LIMIT = 4096
DIFF_THRESHOLD = 0.10       # +/-10% value change counts as a resize
DUST = 1_000_000            # positions below $1M are collapsed / ignored in diffs


# ----------------------------- HTTP helpers -----------------------------

def http_get(url, as_json=False, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                if "gzip" in r.headers.get("Content-Encoding", ""):
                    import gzip
                    raw = gzip.decompress(raw)
                data = raw.decode("utf-8", "replace")
            time.sleep(0.2)  # polite (SEC asks <= 10 req/s)
            return json.loads(data) if as_json else data
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1 + attempt)
    raise RuntimeError(f"GET failed after {retries} tries: {url} ({last})")


# ----------------------------- state / config -----------------------------

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")


# ----------------------------- EDGAR -----------------------------

def recent_filings(cik):
    d = http_get(SUBMISSIONS.format(cik=cik), as_json=True)
    f = d["filings"]["recent"]
    out = []
    for i in range(len(f["accessionNumber"])):
        out.append({
            "accession": f["accessionNumber"][i],
            "form": f["form"][i],
            "filed": f["filingDate"][i],
            "period": f["reportDate"][i],
            "primaryDoc": f["primaryDocument"][i],
            "desc": f["primaryDocDescription"][i],
        })
    return d.get("name", ""), out


def form_matches(form, wanted):
    if not wanted or "*" in wanted:
        return True
    return form in wanted


def archive_urls(cik, accession):
    base = {"cik_int": int(cik), "acc_nodash": accession.replace("-", ""), "accession": accession}
    return INDEX_JSON.format(**base), FILING_INDEX.format(**base), ARCHIVE_DIR.format(**base)


def find_info_table_url(cik, accession):
    """Locate the 13F information-table XML within a filing directory."""
    idx_url, _, base = archive_urls(cik, accession)
    d = http_get(idx_url, as_json=True)
    candidates = [it["name"] for it in d["directory"]["item"]
                  if it["name"].lower().endswith(".xml") and it["name"].lower() != "primary_doc.xml"]
    for name in candidates:
        url = base + name
        try:
            txt = http_get(url)
        except Exception:  # noqa: BLE001
            continue
        if "informationTable" in txt or "<infoTable" in txt:
            return url, txt
    if candidates:
        url = base + candidates[0]
        return url, http_get(url)
    raise RuntimeError(f"no information table found in {accession}")


def parse_info_table(xml_text):
    """Parse a 13F info table -> ({(cusip, putCall): {value, shares, name}}, total).

    Keyed on CUSIP (stable) + put/call; EDGAR varies issuer-name casing
    between filings, so the name is kept only for display.
    """
    root = ET.fromstring(xml_text)
    for el in root.iter():  # strip namespaces -> local tag names
        el.tag = el.tag.split("}")[-1]
    agg = {}
    total = 0
    for row in root.findall(".//infoTable"):
        issuer = (row.findtext("nameOfIssuer") or "").strip()
        cusip = (row.findtext("cusip") or "").strip().upper()
        put_call = (row.findtext("putCall") or "LONG").strip() or "LONG"
        value = int(float(row.findtext("value") or 0))
        shares = 0
        sp = row.find("shrsOrPrnAmt")
        if sp is not None:
            shares = int(float(sp.findtext("sshPrnamt") or 0))
        key = (cusip, put_call)
        cur = agg.setdefault(key, {"value": 0, "shares": 0, "name": issuer})
        cur["value"] += value
        cur["shares"] += shares
        total += value
    return agg, total


def prior_13f(filings, current_accession):
    seen_current = False
    for f in filings:  # newest first
        if f["accession"] == current_accession:
            seen_current = True
            continue
        if seen_current and f["form"].startswith("13F"):
            return f
    return None


# ----------------------------- formatting -----------------------------

def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def money(v):
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1e9:
        return f"{sign}${a/1e9:.2f}B"
    if a >= 1e6:
        return f"{sign}${a/1e6:.1f}M"
    return f"{sign}${a:,.0f}"


_NAME_TOKENS = {"Etf": "ETF", "Nv": "NV", "Ny": "NY", "Ltd": "Ltd", "Llc": "LLC",
                "Lp": "LP", "Plc": "PLC", "Ai": "AI", "Usa": "USA", "Hldg": "Hldg"}


def nicename(s):
    return " ".join(_NAME_TOKENS.get(w, w) for w in s.title().split())


def _groups(now):
    g = {"LONG": [], "Call": [], "Put": []}
    sub = {"LONG": 0, "Call": 0, "Put": 0}
    for k, v in now.items():
        pc = k[1] if k[1] in g else "LONG"
        g[pc].append((k, v))
        sub[pc] += v["value"]
    for pc in g:
        g[pc].sort(key=lambda kv: -kv[1]["value"])
    return g, sub


def _section(title, rows, subtotal):
    out = ["", f"<b>{title}</b> — {money(subtotal)} · {len(rows)}"]
    for k, v in rows:
        out.append(f"• {esc(nicename(v['name']))} — {money(v['value'])}")
    return out


def _tag(k):
    return {"LONG": " stock", "Put": " put", "Call": " call"}.get(k[1], "")


def _changes(now, prior):
    keys = set(now) | set(prior)
    new, exited, inc, dec = [], [], [], []
    for k in keys:
        a = now.get(k, {}).get("value", 0)
        b = prior.get(k, {}).get("value", 0)
        a_m, b_m = a >= DUST, b >= DUST
        if a_m and not b_m:
            new.append(k)
        elif b_m and not a_m:
            exited.append(k)
        elif a_m and b_m and abs(a - b) / b > DIFF_THRESHOLD:
            (inc if a > b else dec).append((k, b, a))
    new.sort(key=lambda k: -now[k]["value"])
    exited.sort(key=lambda k: -prior[k]["value"])
    inc.sort(key=lambda x: -(x[2] - x[1]))
    dec.sort(key=lambda x: (x[2] - x[1]))  # most negative first

    out = ["", "<b>Changes vs prior quarter</b>"]
    out.append(f"\U0001F195 New ({len(new)}):" + ("" if new else " —"))
    for k in new:
        out.append(f"• {esc(nicename(now[k]['name']))}{_tag(k)} — {money(now[k]['value'])}")
    out.append(f"❌ Exited ({len(exited)}):" + ("" if exited else " —"))
    for k in exited:
        out.append(f"• {esc(nicename(prior[k]['name']))}{_tag(k)} — {money(prior[k]['value'])}")
    out.append(f"⬆️ Increased ({len(inc)}):" + ("" if inc else " —"))
    for k, b, a in inc:
        out.append(f"• {esc(nicename(now[k]['name']))}{_tag(k)}: {money(b)}→{money(a)} ({(a-b)/b*100:+.0f}%)")
    out.append(f"⬇️ Decreased ({len(dec)}):" + ("" if dec else " —"))
    for k, b, a in dec:
        out.append(f"• {esc(nicename(now[k]['name']))}{_tag(k)}: {money(b)}→{money(a)} ({(a-b)/b*100:+.0f}%)")
    return out


def build_13f_message(entity_name, filing, cik, filings):
    acc = filing["accession"]
    last_url = archive_urls(cik, acc)[1]
    _, it = find_info_table_url(cik, acc)
    now, _ = parse_info_table(it)
    g, sub = _groups(now)

    lines = [
        f"\U0001F6A8 <b>{esc(entity_name)}</b> — new 13F-HR",
        f"Filed {filing['filed']} · Reported {filing['period']}",
    ]
    lines += _section("\U0001F4C8 STOCKS", g["LONG"], sub["LONG"])
    lines += _section("\U0001F7E2 CALLS", g["Call"], sub["Call"])
    lines += _section("\U0001F534 PUTS", g["Put"], sub["Put"])

    prior = prior_13f(filings, acc)
    if prior:
        try:
            _, pit = find_info_table_url(cik, prior["accession"])
            pa, _ = parse_info_table(pit)
            lines += _changes(now, pa)
        except Exception as e:  # noqa: BLE001
            lines += ["", f"(diff unavailable: {esc(str(e))})"]

    if prior:
        prev_url = archive_urls(cik, prior["accession"])[1]
        link = f'Filings: <a href="{last_url}">Last</a> · <a href="{prev_url}">Previous</a>'
    else:
        link = f'Filings: <a href="{last_url}">Last</a>'
    lines += ["", link]
    return "\n".join(lines)


def build_generic_message(entity_name, filing, cik):
    idx_url = archive_urls(cik, filing["accession"])[1]
    desc = filing["desc"] or filing["form"]
    return "\n".join([
        f"\U0001F4C4 <b>New filing</b> — {esc(entity_name)}",
        f"Form: <b>{esc(filing['form'])}</b>" + (f" · {esc(filing['period'])}" if filing["period"] else ""),
        f"Filed: {filing['filed']}",
        f"{esc(desc)}",
        f'<a href="{idx_url}">EDGAR ↗</a>',
    ])


# ----------------------------- Telegram -----------------------------

def _chunks(text, limit=TG_LIMIT):
    """Split on line boundaries so an HTML tag is never broken mid-message."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # pathological single long line
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = line if not cur else cur + "\n" + line
    if cur:
        chunks.append(cur)
    return chunks


def send_telegram(text, dry=False):
    if dry:
        print("---- TELEGRAM (dry) ----\n" + text + "\n------------------------")
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        # Hard-fail rather than silently swallow: keeps the filing from being
        # marked "seen", so it re-alerts once the secrets are configured.
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
    for chunk in _chunks(text, TG_LIMIT):
        payload = urllib.parse.urlencode({
            "chat_id": chat,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(TG_API.format(token=token, method="sendMessage"), data=payload)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                r.read()
        except urllib.error.HTTPError as e:
            print(f"Telegram error {e.code}: {e.read().decode('utf-8','replace')}")
            raise
        time.sleep(0.3)


# ----------------------------- main -----------------------------

def process_entity(entity, state, mode):
    cik = str(entity["cik"]).lstrip("0") or "0"
    cik_key = str(entity["cik"])
    wanted = entity.get("forms", ["*"])

    feed_name, filings = recent_filings(cik)
    entity_name = entity.get("name", "") or feed_name
    st = state.setdefault(cik_key, {"name": entity_name, "seen": [], "last_filed": None})
    st["name"] = entity_name
    seen = set(st["seen"])

    new_filings = [f for f in filings if form_matches(f["form"], wanted) and f["accession"] not in seen]

    if mode == "seed":
        for f in filings:
            seen.add(f["accession"])
        st["seen"] = sorted(seen)
        if filings:
            st["last_filed"] = filings[0]["filed"]
        print(f"[seed] {entity_name}: marked {len(filings)} filings as seen (no alerts)")
        return False

    changed = False
    for f in reversed(new_filings):  # oldest first -> chronological alerts
        try:
            if f["form"].startswith("13F"):
                msg = build_13f_message(entity_name, f, cik, filings)
            else:
                msg = build_generic_message(entity_name, f, cik)
        except Exception as e:  # noqa: BLE001 — fall back to a bare alert, never crash
            idx_url = archive_urls(cik, f["accession"])[1]
            msg = (f"\U0001F4C4 <b>New filing</b> — {esc(entity_name)}\n"
                   f"Form: <b>{esc(f['form'])}</b> · Filed: {f['filed']}\n"
                   f'<a href="{idx_url}">EDGAR ↗</a>\n(enrichment failed: {esc(str(e))})')

        send_telegram(msg, dry=(mode == "dry"))
        seen.add(f["accession"])
        changed = True

    if new_filings and mode != "dry":
        st["seen"] = sorted(seen)
        st["last_filed"] = filings[0]["filed"]
    if not new_filings:
        print(f"[ok] {entity_name}: no new filings")
    return changed


def main():
    args = set(sys.argv[1:])
    if "--test" in args:
        send_telegram("✅ <b>sec-filing-alerts</b> test message — the Telegram pipe works.")
        return
    if "--demo" in args:
        # Re-send the latest 13F for each entity WITHOUT touching state
        # (idempotent; no commit, no cron race). For previewing the format.
        for entity in load_json(WATCHLIST, []):
            cik = str(entity["cik"]).lstrip("0") or "0"
            feed_name, filings = recent_filings(cik)
            last = next((f for f in filings if f["form"].startswith("13F")), None)
            if last:
                send_telegram(build_13f_message(entity.get("name") or feed_name, last, cik, filings))
                print(f"[demo] sent {entity.get('name')} {last['accession']}")
        return
    mode = "seed" if "--seed" in args else ("dry" if "--dry-run" in args else "normal")

    watchlist = load_json(WATCHLIST, [])
    state = load_json(STATE, {})
    any_changed = False
    for entity in watchlist:
        try:
            if process_entity(entity, state, mode):
                any_changed = True
        except Exception as e:  # noqa: BLE001
            print(f"ERROR processing {entity.get('name', entity.get('cik'))}: {e}")

    if mode == "seed" or (any_changed and mode == "normal"):
        save_json(STATE, state)
        print("state.json updated")
    print("STATE_CHANGED=" + ("1" if (mode == "seed" or any_changed) else "0"))


if __name__ == "__main__":
    main()
