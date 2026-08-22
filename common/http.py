"""HTTP helpers shared by the watchers.

Two User-Agents, deliberately. SEC's WAF rejects a UA containing a URL and
wants a descriptive contact string; whitehouse.gov serves the OGE disclosure
index only to a browser-shaped UA. Don't unify them.
"""

import json
import time
import urllib.request

SEC_UA = "sec-filing-alerts mrtsfn601 maratsafin601@gmail.com"
BROWSER_UA = "Mozilla/5.0 (sec-filing-alerts; maratsafin601@gmail.com)"


def http_get(url, as_json=False, retries=3, ua=SEC_UA, timeout=30):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
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


def http_bytes(url, retries=3, ua=SEC_UA, timeout=60):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1 + attempt)
    raise RuntimeError(f"GET failed: {url} ({last})")
