"""Free public observations, independent of the trading processes.

RSS summaries are partial coverage; publication times are source declarations.
Each capture uses its actual fetch time as first_seen, never a backdated time.
"""
import hashlib
import json
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.request import Request, urlopen
from .market_research import ResearchError, INTERVALS_MS


FEEDS = {
    "federal_reserve": "https://www.federalreserve.gov/feeds/press_all.xml",
    "ecb": "https://www.ecb.europa.eu/rss/press.html",
}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain(text):
    parser = _Text()
    parser.feed(text or "")
    return " ".join(" ".join(parser.parts).split())


def public_get(url):
    req = Request(url, headers={"User-Agent": "TradingBot-Paper-Research/1.0"}, method="GET")
    try:
        with urlopen(req, timeout=15) as response:
            body = response.read(1000001)
        if len(body) > 1000000:
            raise ResearchError("public_response_too_large")
        return body
    except Exception:
        raise ResearchError("public_source_unavailable") from None


def parse_feed(body, publisher, observed_at_ms):
    if len(body) > 1000000 or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise ResearchError("unsafe_feed_xml")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise ResearchError("invalid_feed_xml") from None
    result = []
    seen = set()
    for item in root.findall(".//item")[:30]:
        url = (item.findtext("link") or "").strip()
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
            if published.tzinfo is None:
                continue
            published_ms = int(published.timestamp() * 1000)
        except (ValueError, TypeError, OverflowError):
            continue
        title = plain(item.findtext("title"))[:300]
        text = plain(item.findtext("description"))[:4000]
        if title and not text:
            text = "HEADLINE ONLY; article body was not fetched: " + title
        if not title or not text or not url.startswith("https://") or published_ms > observed_at_ms or published_ms < 0:
            continue
        ident = "rss-" + hashlib.sha256(url.encode()).hexdigest()[:24]
        if ident in seen:
            continue
        seen.add(ident)
        result.append({"id": ident, "publisher": publisher, "url": url,
                       "title": title, "text": text, "published_at_ms": published_ms,
                       "first_seen_at_ms": observed_at_ms, "source_type": "primary", "assets": ["MARKET"]})
    return result


def capture_public(*, interval="15m", getter=public_get, clock=time.time):
    if interval not in INTERVALS_MS:
        raise ResearchError("unsupported_interval")
    markets = {}
    for symbol in ("BTCUSDT", "ETHUSDT"):
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit=61"
        try:
            candles = json.loads(getter(url))
        except (ValueError, TypeError):
            raise ResearchError("invalid_public_candles") from None
        markets[symbol] = {"source": "binance_public_spot", "observed_at_ms": int(clock()*1000), "candles": candles}
    news = []
    status = {}
    for publisher, url in FEEDS.items():
        try:
            body = getter(url)
            articles = parse_feed(body, publisher, int(clock()*1000))
            news.extend(articles)
            status[publisher] = {"status": "summaries_received" if articles else "no_usable_items", "count": len(articles)}
        except ResearchError:
            status[publisher] = {"status": "unavailable", "count": 0}
    as_of = int(clock()*1000)
    news = list({r["id"]: r for r in news}.values())[:50]
    return {"version": 1, "as_of_ms": as_of, "captured_at_ms": as_of,
            "interval": interval, "markets": markets, "news": news, "fundamentals": [],
            "collection_status": status}
