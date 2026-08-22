"""Display formatting shared across the alert builders."""


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


def num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, AttributeError):
        return 0.0


def isodate(s):
    """'06/22/2026' -> '2026-06-22' (leave other formats unchanged)."""
    parts = (s or "").strip().split("/")
    if len(parts) == 3 and parts[2].isdigit():
        mm, dd, yy = parts
        try:
            return f"{yy}-{int(mm):02d}-{int(dd):02d}"
        except ValueError:
            return s
    return s
