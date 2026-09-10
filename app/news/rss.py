"""RSS news collector + deduplication + currency mapping + importance filter."""
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

# Curated, mainstream sources (public RSS). Verified working feeds first;
# dead/blocked feeds removed (fxstreet & reuters agency return 0 items).
FEEDS = {
    "forexlive": "https://www.forexlive.com/feed/news",
    "investing_forex": "https://www.investing.com/rss/news_1.rss",
    "yahoo_finance": "https://finance.yahoo.com/news/rssindex",
    "marketwatch": "https://feeds.marketwatch.com/marketwatch/topstories/",
}

CURRENCY_KEYWORDS = {
    "USD": ["dollar", "usd", "fed", "fomc", "powell", "u.s.", "united states", "nfp",
            "nonfarm", "cpi", "inflation"],
    "EUR": ["euro", "ecb", "lagarde", "eurozone", "eur"],
    "GBP": ["pound", "sterling", "boe", "bailey", "uk ", "britain", "gbp"],
    "JPY": ["yen", "boj", "japan", "ueda", "jpy"],
    "CHF": ["franc", "snb", "swiss", "chf"],
    "AUD": ["australian dollar", "rba", "australia", "aud"],
    "CAD": ["loonie", "boc", "canada", "cad", "crude"],
}

HIGH_IMPACT_KEYWORDS = [
    "rate decision", "interest rate", "fomc", "nfp", "nonfarm", "cpi", "inflation",
    "gdp", "unemployment", "central bank", "intervention", "emergency",
]
MED_IMPACT_KEYWORDS = ["pmi", "retail sales", "consumer confidence", "trade balance",
                       "housing", "speech", "minutes"]


def _text(el, tag, ns=""):
    node = el.find(f"{ns}{tag}")
    return (node.text or "").strip() if node is not None else ""


def fetch_feed(url: str, timeout: int = 15):
    """Parse RSS/Atom into items. Returns [] on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 ai-trader"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(262144)  # 256KB cap
    except Exception:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    items = []
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"
    entries = root.findall(f".//{ns}item") or root.findall(f".//{ns}entry")
    for it in entries:
        title = _text(it, f"{ns}title") or _text(it, "title")
        link = _text(it, f"{ns}link")
        if not link:
            lel = it.find(f"{ns}link")
            link = lel.get("href") if lel is not None else ""
        desc = _text(it, f"{ns}description") or _text(it, f"{ns}summary")
        pub = _text(it, f"{ns}pubDate") or _text(it, f"{ns}updated")
        if title:
            items.append({"title": title, "url": link, "summary": desc[:300],
                          "published_raw": pub})
    return items


def parse_date(raw: str):
    if not raw:
        return None
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).astimezone(timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def classify(item: dict) -> dict:
    text = (item["title"] + " " + item.get("summary", "")).lower()
    currencies = [c for c, kws in CURRENCY_KEYWORDS.items()
                  if any(k in text for k in kws)]
    tl = text
    if any(k in tl for k in HIGH_IMPACT_KEYWORDS):
        importance = "HIGH"
    elif any(k in tl for k in MED_IMPACT_KEYWORDS):
        importance = "MEDIUM"
    else:
        importance = "LOW"
    return {"currencies": currencies, "importance": importance}


def dedupe(items):
    seen, out = set(), []
    for it in items:
        key = re.sub(r"[^a-z0-9]", "", it["title"].lower())[:80]
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def collect(feeds=None, min_importance: str = "MEDIUM", limit_per_feed: int = 25):
    """Full pipeline: fetch -> parse -> dedupe -> classify -> filter."""
    order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    feeds = feeds or FEEDS
    all_items = []
    for source, url in feeds.items():
        for it in fetch_feed(url)[:limit_per_feed]:
            it["source"] = source
            it["published_ts"] = parse_date(it.pop("published_raw", None))
            it.update(classify(it))
            all_items.append(it)
    all_items = dedupe(all_items)
    return [it for it in all_items
            if order.get(it["importance"], 0) >= order.get(min_importance, 1)][:60]
