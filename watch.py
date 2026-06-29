#!/usr/bin/env python3
"""
sec-filing-alerts — poll SEC EDGAR for new filings by watched entities and
push a Telegram alert. For 13F-HR filings the alert includes a deterministic
holdings table + diff vs the prior quarter (parsed in Python, no LLM).

Generalizable: add any entity to watchlist.json as {name, cik, forms}.
  forms: ["*"]            -> alert on every form
  forms: ["13F-HR", ...]  -> alert only on the listed form types (exact EDGAR strings)

Usage:
  python watch.py            # normal run: detect new filings, alert, update state
  python watch.py --seed     # mark all current filings as seen, send NO alerts (setup)
  python watch.py --test     # send a one-off test message to confirm the Telegram pipe
  python watch.py --dry-run  # detect + print to stdout, do not send Telegram, do not save

Env (from GitHub Actions secrets; never hard-coded):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST = os.path.join(HERE, "watchlist.json")
STATE = os.path.join(HERE, "state.json")

# SEC fair-access policy requires a descriptive User-Agent (this is NOT a secret).
UA = "sec-filing-alerts mrtsfn601 maratsafin601@gmail.com"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"
ARCHIVE_DIR = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/"
INDEX_JSON = ARCHIVE_DIR + "index.json"
FILING_INDEX = ARCHIVE_DIR + "{accession}-index.html"

TG_API = "https://api.telegram.org/bot{token}/{method}"
TG_LIMIT = 4096
DIFF_THRESHOLD = 0.10  # +/-10% value change counts as a resize


# ----------------------------- HTTP helpers -----------------------------

def http_get(url, as_json=False, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                enc = r.headers.get("Content-Encoding", "")
                if "gzip" in enc:
                    import gzip
                    raw = gzip.decompress(raw)
                data = raw.decode("utf-8", "replace")
            time.sleep(0.2)  # be polite (SEC asks <= 10 req/s)
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
    """Return list of filing dicts (newest first) from the submissions feed."""
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
    cik_int = int(cik)
    acc_nodash = accession.replace("-", "")
    base = {"cik_int": cik_int, "acc_nodash": acc_nodash, "accession": accession}
    return INDEX_JSON.format(**base), FILING_INDEX.format(**base), ARCHIVE_DIR.format(**base)


def find_info_table_url(cik, accession):
    """Locate the 13F information-table XML within a filing directory."""
    idx_url, _, base = archive_urls(cik, accession)
    d = http_get(idx_url, as_json=True)
    candidates = []
    for item in d["directory"]["item"]:
        name = item["name"]
        if name.lower().endswith(".xml") and name.lower() != "primary_doc.xml":
            candidates.append(name)
    # Prefer the file whose content actually contains an informationTable.
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
    """Parse a 13F info table -> (aggregated {(cusip, putCall): {value, shares, name}}, total).

    Keyed on CUSIP (stable across quarters) + put/call, because EDGAR varies
    issuer-name casing between filings; the issuer name is kept only for display.
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
    """The most recent 13F-HR(/A) older than the current one."""
    seen_current = False
    for f in filings:  # newest first
        if f["accession"] == current_accession:
            seen_current = True
            continue
        if seen_current and f["form"].startswith("13F"):
            return f
    return None


# ----------------------------- message building -----------------------------

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


def build_13f_message(entity_name, filing, cik):
    idx_url = archive_urls(cik, filing["accession"])[1]
    _, it_text = find_info_table_url(cik, filing["accession"])
    now, total_now = parse_info_table(it_text)

    lines = [
        f"\U0001F6A8 <b>New 13F-HR</b> — {esc(entity_name)}",
        f"Period: <b>{filing['period']}</b> · Filed: {filing['filed']}",
        f"Portfolio value: <b>{money(total_now)}</b> · Positions: <b>{len(now)}</b>",
    ]

    return lines, now, total_now, idx_url


def _label(agg, k):
    pc = k[1]
    name = agg[k]["name"]
    return esc(name) + ("" if pc == "LONG" else f" [{pc.upper()}]")


def render_holdings(now, total_now, topn=10):
    rows = sorted(now.items(), key=lambda kv: -kv[1]["value"])[:topn]
    out = ["", f"<b>Top {min(topn, len(now))} holdings</b>:"]
    for k, d in rows:
        pct = 100 * d["value"] / total_now if total_now else 0
        out.append(f"• {_label(now, k)} — {money(d['value'])} ({pct:.1f}%)")
    return out


def render_diff(now, prior):
    now_keys, prior_keys = set(now), set(prior)
    new = sorted(now_keys - prior_keys, key=lambda k: -now[k]["value"])
    exited = sorted(prior_keys - now_keys, key=lambda k: -prior[k]["value"])
    up, down = [], []
    for k in now_keys & prior_keys:
        a, b = now[k]["value"], prior[k]["value"]
        if b and (a - b) / b > DIFF_THRESHOLD:
            up.append((k, a - b))
        elif b and (a - b) / b < -DIFF_THRESHOLD:
            down.append((k, a - b))
    up.sort(key=lambda x: -x[1])
    down.sort(key=lambda x: x[1])

    def label(k):
        agg = now if k in now else prior
        return _label(agg, k)

    out = ["", "<b>Changes vs prior quarter</b>:"]
    out.append(f"\U0001F195 New ({len(new)}): " + (", ".join(label(k) for k in new[:8]) or "—"))
    out.append(f"❌ Exited ({len(exited)}): " + (", ".join(label(k) for k in exited[:8]) or "—"))
    out.append(f"⬆️ Increased ({len(up)}): " + (", ".join(f"{label(k)} +{money(v)}" for k, v in up[:6]) or "—"))
    out.append(f"⬇️ Decreased ({len(down)}): " + (", ".join(f"{label(k)} {money(v)}" for k, v in down[:6]) or "—"))
    return out


def build_generic_message(entity_name, filing, cik):
    idx_url = archive_urls(cik, filing["accession"])[1]
    desc = filing["desc"] or filing["form"]
    lines = [
        f"\U0001F4C4 <b>New filing</b> — {esc(entity_name)}",
        f"Form: <b>{esc(filing['form'])}</b>" + (f" · {esc(filing['period'])}" if filing["period"] else ""),
        f"Filed: {filing['filed']}",
        f"{esc(desc)}",
        f'<a href="{idx_url}">View on EDGAR</a>',
    ]
    return "\n".join(lines)


# ----------------------------- Telegram -----------------------------

def send_telegram(text, dry=False):
    if dry:
        print("---- TELEGRAM (dry) ----\n" + text + "\n------------------------")
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("WARN: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; printing instead:\n" + text)
        return
    for i in range(0, len(text), TG_LIMIT):
        chunk = text[i:i + TG_LIMIT]
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
    name_cfg = entity.get("name", "")
    wanted = entity.get("forms", ["*"])

    feed_name, filings = recent_filings(cik)
    entity_name = name_cfg or feed_name
    st = state.setdefault(cik_key, {"name": entity_name, "seen": [], "last_filed": None})
    st["name"] = entity_name
    seen = set(st["seen"])

    # candidates = matching, not-yet-seen, newest first
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
    # oldest-first so alerts arrive chronologically
    for f in reversed(new_filings):
        try:
            if f["form"].startswith("13F"):
                lines, now, total_now, idx_url = build_13f_message(entity_name, f, cik)
                lines += render_holdings(now, total_now)
                prior = prior_13f(filings, f["accession"])
                if prior:
                    try:
                        _, pit = find_info_table_url(cik, prior["accession"])
                        prior_agg, _ = parse_info_table(pit)
                        lines += render_diff(now, prior_agg)
                    except Exception as e:  # noqa: BLE001
                        lines += ["", f"(diff unavailable: {esc(str(e))})"]
                lines += ["", f'<a href="{idx_url}">View on EDGAR</a>']
                msg = "\n".join(lines)
            else:
                msg = build_generic_message(entity_name, f, cik)
        except Exception as e:  # noqa: BLE001 — fall back to a bare alert, never crash the run
            idx_url = archive_urls(cik, f["accession"])[1]
            msg = (f"\U0001F4C4 <b>New filing</b> — {esc(entity_name)}\n"
                   f"Form: <b>{esc(f['form'])}</b> · Filed: {f['filed']}\n"
                   f'<a href="{idx_url}">View on EDGAR</a>\n(enrichment failed: {esc(str(e))})')

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
    mode = "normal"
    if "--seed" in args:
        mode = "seed"
    elif "--dry-run" in args:
        mode = "dry"

    if "--test" in args:
        send_telegram("✅ <b>sec-filing-alerts</b> test message — the Telegram pipe works.")
        return

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
    # Emit a flag the workflow can grep to decide whether to commit.
    print("STATE_CHANGED=" + ("1" if (mode == "seed" or any_changed) else "0"))


if __name__ == "__main__":
    main()
