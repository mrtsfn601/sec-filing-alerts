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
    # substring match so e.g. "13D" catches "SCHEDULE 13D", "SC 13D/A", etc.
    return any(w in form for w in wanted)


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

    lines += ["", f'<a href="{last_url}">Filing ↗</a>']
    return "\n".join(lines)


def _num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, AttributeError):
        return 0.0


def _isodate(s):
    """'06/22/2026' -> '2026-06-22' (leave other formats unchanged)."""
    parts = (s or "").strip().split("/")
    if len(parts) == 3 and parts[2].isdigit():
        mm, dd, yy = parts
        try:
            return f"{yy}-{int(mm):02d}-{int(dd):02d}"
        except ValueError:
            return s
    return s


def build_ownership_message(entity_name, filing, cik):
    """13D / 13G beneficial-ownership filings (structured XML, post-2024)."""
    _, idx_url, base = archive_urls(cik, filing["accession"])
    root = ET.fromstring(http_get(base + "primary_doc.xml"))
    for el in root.iter():
        el.tag = el.tag.split("}")[-1]

    def ft(tag):
        el = root.find(f".//{tag}")
        return (el.text or "").strip() if (el is not None and el.text) else ""

    subtype = ft("submissionType") or filing["form"]
    short = subtype.replace("SCHEDULE ", "").replace("SC ", "").strip() or subtype  # -> 13G, 13G/A, 13D
    amend = ft("amendmentNo")
    if amend:
        short = f"{short} #{amend}"
    issuer = ft("issuerName")
    cls = ft("securitiesClassTitle")
    # field names differ between the 13G and 13D XML schemas — try both
    event = ft("eventDateRequiresFilingThisStatement") or ft("dateOfEvent")
    shares = max([_num(e.text) for e in root.iter("reportingPersonBeneficiallyOwnedAggregateNumberOfShares")]
                 + [_num(e.text) for e in root.iter("aggregateAmountOwned")] + [0.0])
    pct = max([_num(e.text) for e in root.iter("classPercent")]
              + [_num(e.text) for e in root.iter("percentOfClass")] + [0.0])

    hdr2 = f"Filed {filing['filed']}"
    if filing.get("period"):
        hdr2 += f" · Reported {filing['period']}"
    if event:
        hdr2 += f" · Event date {_isodate(event)}"
    lines = [
        f"\U0001F6A8 <b>{esc(entity_name)}</b> — new {esc(short)}",
        hdr2,
        "",
        "<b>\U0001F4C8 STOCKS</b>",
    ]
    stake = []
    if shares:
        stake.append(f"{int(shares):,} sh")
    if pct:
        stake.append(f"{pct:g}% of {esc(cls) if cls else 'class'}")
    lines.append(f"• <b>{esc(issuer)}</b> — " + (" · ".join(stake) if stake else "—"))
    lines += ["", f'<a href="{idx_url}">Filing ↗</a>']
    return "\n".join(lines)


# ------------------- Form 3/4/5 (insider transactions) -------------------

# SEC ownership transaction codes -> (emoji, group label). Direction (buy/sell)
# comes from the code itself; acquired/disposed is a cross-check only.
_TXN_CODES = {
    "P": ("\U0001F7E2", "BUY"),                 # open-market / private purchase
    "S": ("\U0001F534", "SELL"),                # open-market / private sale
    "A": ("\U0001F381", "GRANT / AWARD"),       # comp grant (RSU/option/stock)
    "M": ("⚙️", "OPTION EXERCISE"),   # exercise/conversion of derivative
    "X": ("⚙️", "OPTION EXERCISE"),
    "C": ("\U0001F501", "CONVERSION"),
    "F": ("\U0001F3E6", "TAX WITHHOLDING"),     # shares withheld to pay tax/strike
    "G": ("\U0001F381", "GIFT"),
    "D": ("\U0001F53B", "DISPOSED TO ISSUER"),
    "V": ("\U0001F7E2", "BUY"),
    "J": ("•", "OTHER"),
}

_F4_LABEL = {"4": "Form 4", "4/A": "Form 4/A", "3": "Form 3", "3/A": "Form 3/A",
             "5": "Form 5", "5/A": "Form 5/A"}


def find_ownership_xml(cik, accession):
    """Locate the raw ownershipDocument XML in a Form 3/4/5 filing directory."""
    idx_url, _, base = archive_urls(cik, accession)
    d = http_get(idx_url, as_json=True)
    xmls = [it["name"] for it in d["directory"]["item"]
            if it["name"].lower().endswith(".xml") and not it["name"].lower().startswith("xsl")]
    for name in xmls:
        url = base + name
        try:
            txt = http_get(url)
        except Exception:  # noqa: BLE001
            continue
        if "<ownershipDocument" in txt:
            return url, txt
    raise RuntimeError(f"no ownership XML in {accession}")


def _short_sec(title):
    return (title or "").split(",")[0].strip() or (title or "").strip()


def _f4_txns(root):
    """All non-derivative + derivative transactions as flat dicts."""
    out = []
    for table, tag in (("nonDerivativeTable", "nonDerivativeTransaction"),
                       ("derivativeTable", "derivativeTransaction")):
        tbl = root.find(table)
        if tbl is None:
            continue
        for t in tbl.findall(tag):
            out.append({
                "title": (t.findtext("securityTitle/value") or "").strip(),
                "date": (t.findtext("transactionDate/value") or "").strip(),
                "code": (t.findtext("transactionCoding/transactionCode") or "").strip(),
                "shares": _num(t.findtext("transactionAmounts/transactionShares/value")),
                "price": _num(t.findtext("transactionAmounts/transactionPricePerShare/value")),
                "ad": (t.findtext("transactionAmounts/transactionAcquiredDisposedCode/value") or "").strip(),
                "held": _num(t.findtext("postTransactionAmounts/sharesOwnedFollowingTransaction/value")),
                "di": (t.findtext("ownershipNature/directOrIndirectOwnership/value") or "").strip(),
                "deriv": table.startswith("deriv"),
            })
    return out


def _f4_holdings(root):
    """Static holdings (Form 3, or holding rows on a Form 4) as flat dicts."""
    out = []
    for table, tag in (("nonDerivativeTable", "nonDerivativeHolding"),
                       ("derivativeTable", "derivativeHolding")):
        tbl = root.find(table)
        if tbl is None:
            continue
        for h in tbl.findall(tag):
            out.append({
                "title": _short_sec(h.findtext("securityTitle/value") or ""),
                "shares": _num(h.findtext("postTransactionAmounts/sharesOwnedFollowingTransaction/value")),
                "di": (h.findtext("ownershipNature/directOrIndirectOwnership/value") or "").strip(),
            })
    return out


def _own(di):
    return {"D": "direct", "I": "indirect"}.get(di, "")


def build_form4_message(entity_name, filing, cik):
    """Form 3/4/5 insider statement -> grouped BUY/SELL/GRANT/... alert."""
    _, xml = find_ownership_xml(cik, filing["accession"])
    root = ET.fromstring(xml)  # ownership docs carry no XML namespace
    doctype = (root.findtext("documentType") or filing["form"]).strip()
    issuer = (root.findtext("issuer/issuerName") or "").strip()
    ticker = (root.findtext("issuer/issuerTradingSymbol") or "").strip()

    rel = root.find("reportingOwner/reportingOwnerRelationship")
    roles = []
    if rel is not None:
        if (rel.findtext("isDirector") or "0") in ("1", "true"):
            roles.append("Director")
        if (rel.findtext("isOfficer") or "0") in ("1", "true"):
            roles.append((rel.findtext("officerTitle") or "Officer").strip() or "Officer")
        if (rel.findtext("isTenPercentOwner") or "0") in ("1", "true"):
            roles.append("10% owner")
        if (rel.findtext("isOther") or "0") in ("1", "true"):
            roles.append("Insider")

    label = _F4_LABEL.get(doctype, f"Form {doctype}")
    head = f"\U0001F9D1‍\U0001F4BC <b>{esc(entity_name)}</b> — new {label}"
    sub = f"<b>{esc(ticker)}</b> · {esc(nicename(issuer))}" if ticker else f"<b>{esc(issuer)}</b>"
    third = f"Filed {filing['filed']}" + (f" · {esc(' · '.join(roles))}" if roles else "")
    lines = [head, sub, third]

    txns = _f4_txns(root)
    if txns:
        groups, order = {}, []
        for x in txns:
            key = _TXN_CODES.get(x["code"], ("•", x["code"] or "OTHER"))
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(x)
        for emoji, verb in order:
            lines += ["", f"{emoji} <b>{verb}</b>"]
            for x in groups[(emoji, verb)]:
                deriv = " [deriv]" if x["deriv"] else ""
                seg = f"• <b>{esc(_short_sec(x['title']))}</b>{deriv} — {x['shares']:,.0f} sh"
                if x["price"]:
                    seg += f" @ ${x['price']:,.2f} = {money(x['shares'] * x['price'])}"
                mmdd = x["date"][5:] if len(x["date"]) == 10 else x["date"]
                if mmdd:
                    seg += f" · {mmdd}"
                if _own(x["di"]):
                    seg += f" · {_own(x['di'])}"
                if x["held"]:
                    seg += f" → {x['held']:,.0f} held"
                lines.append(seg)
    else:  # Form 3 or holdings-only Form 4/5
        holds = _f4_holdings(root)
        lines += ["", "<b>Holdings</b>"]
        if holds:
            for h in holds:
                seg = f"• <b>{esc(h['title'])}</b> — {h['shares']:,.0f} sh"
                if _own(h["di"]):
                    seg += f" · {_own(h['di'])}"
                lines.append(seg)
        elif (root.findtext("noSecuritiesOwned") or "0") in ("1", "true"):
            lines.append("• No securities beneficially owned")
        else:
            lines.append("(no holdings parsed — see filing)")
        remarks = (root.findtext("remarks") or "").strip()
        if remarks:
            lines.append(f"↳ {esc(remarks[:220])}")

    idx_url = archive_urls(cik, filing["accession"])[1]
    lines += ["", f'<a href="{idx_url}">Filing ↗</a>']
    return "\n".join(lines)


def build_generic_message(entity_name, filing, cik):
    idx_url = archive_urls(cik, filing["accession"])[1]
    desc = filing["desc"] or filing["form"]
    return "\n".join([
        f"\U0001F4C4 <b>New filing</b> — {esc(entity_name)}",
        f"Form: <b>{esc(filing['form'])}</b>" + (f" · {esc(filing['period'])}" if filing["period"] else ""),
        f"Filed: {filing['filed']}",
        f"{esc(desc)}",
        f'<a href="{idx_url}">Filing ↗</a>',
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
            elif f["form"] in ("4", "4/A", "5", "5/A", "3", "3/A"):
                msg = build_form4_message(entity_name, f, cik)
            elif "13D" in f["form"] or "13G" in f["form"]:
                msg = build_ownership_message(entity_name, f, cik)
            else:
                msg = build_generic_message(entity_name, f, cik)
        except Exception as e:  # noqa: BLE001 — fall back to a bare alert, never crash
            idx_url = archive_urls(cik, f["accession"])[1]
            msg = (f"\U0001F4C4 <b>New filing</b> — {esc(entity_name)}\n"
                   f"Form: <b>{esc(f['form'])}</b> · Filed: {f['filed']}\n"
                   f'<a href="{idx_url}">Filing ↗</a>\n(enrichment failed: {esc(str(e))})')

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
        # Re-send each entity's latest 13F AND latest 13D/13G, WITHOUT touching
        # state (idempotent; no commit, no cron race). DEMO_CIK filters to one.
        demo_cik = os.environ.get("DEMO_CIK", "").strip().lstrip("0")
        demo_acc = [a.strip() for a in os.environ.get("DEMO_ACC", "").split(",") if a.strip()]
        for entity in load_json(WATCHLIST, []):
            cik = str(entity["cik"]).lstrip("0") or "0"
            if demo_cik and demo_cik != cik:
                continue
            feed_name, filings = recent_filings(cik)
            name = entity.get("name") or feed_name

            def send_one(f):
                if f["form"].startswith("13F"):
                    msg = build_13f_message(name, f, cik, filings)
                elif f["form"] in ("4", "4/A", "5", "5/A", "3", "3/A"):
                    msg = build_form4_message(name, f, cik)
                elif "13D" in f["form"] or "13G" in f["form"]:
                    msg = build_ownership_message(name, f, cik)
                else:
                    msg = build_generic_message(name, f, cik)
                send_telegram(msg)
                print(f"[demo] {name}: sent {f['form']} {f['accession']}")

            if demo_acc:  # send these exact accessions, in the order requested
                by_acc = {f["accession"]: f for f in filings}
                for a in demo_acc:
                    if a in by_acc:
                        send_one(by_acc[a])
                    else:
                        print(f"[demo] {name}: accession {a} not found")
            else:  # default: latest 13F, 13D, 13G, and Form 4 (whichever exist)
                last_13f = next((f for f in filings if f["form"].startswith("13F")), None)
                last_13d = next((f for f in filings if "13D" in f["form"]), None)
                last_13g = next((f for f in filings if "13G" in f["form"]), None)
                last_f4 = next((f for f in filings if f["form"] in ("4", "4/A")), None)
                for f in (last_13f, last_13d, last_13g, last_f4):
                    if f:
                        send_one(f)
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
