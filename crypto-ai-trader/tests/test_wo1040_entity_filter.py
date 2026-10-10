"""WO-1040 symbol-ambiguity news pollution fix tests.

Evidence base: live Jina searches 2026-10-10 —
  OGN bare query -> Organon & Co. (NYSE pharma), Oil & Gas News
  RLC bare query -> Royal Logistic Corps, Republican Liberty Caucus,
                    RLC Residences (real estate)
All wrong-entity articles below are REAL titles captured from those
searches; the filter must drop every one and keep genuine crypto
articles about the mapped projects.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.news_entity_filter import is_crypto_related  # noqa: E402
from src.coin_names import (  # noqa: E402
    BASE_TO_NAME, news_query_for, symbol_to_coin_name,
)

# --- real captured wrong-entity articles (2026-10-10 live searches) ---
OGN_POLLUTION = [
    {"title": "Organon & Co. (OGN) Latest Stock News & Headlines",
     "url": "https://finance.yahoo.com/quote/OGN/news/", "summary": ""},
    {"title": "Oil & Gas News (OGN)",
     "url": "https://ognnews.com/", "summary": "industry coverage"},
    {"title": "Check out Organon's stock price (OGN) in real time",
     "url": "https://www.cnbc.com/quotes/OGN", "summary": ""},
    {"title": "Organon (OGN) — Buy and sell stocks commission-free",
     "url": "https://robinhood.com/us/en/stocks/OGN/", "summary": ""},
    {"title": "News | Organon",
     "url": "https://www.organon.com/media/press-releases/", "summary": ""},
    {"title": "OGN Stock Price, News & Analysis",
     "url": "https://www.stocktitan.net/overview/OGN/", "summary": ""},
]
RLC_POLLUTION = [
    {"title": "Latest News, Views & Events from The RLC - The Royal Logistic Corps",
     "url": "https://www.royallogisticcorps.co.uk/news/", "summary": "army"},
    {"title": "REPUBLICAN LIBERTY CAUCUS STATEMENT ON PENNSYLVANIA",
     "url": "https://rlc.org/category/news/", "summary": ""},
    {"title": "News and Promotions | RLC Residences",
     "url": "https://rlcresidences.com/news-and-promotions", "summary": "condo"},
    {"title": "News & Events | RLC Global Forum",
     "url": "https://rlcglobalforum.com/retail-insights/category/news-events/",
     "summary": ""},
]
GENUINE = [
    {"title": "Origin Protocol launches OGN staking rewards on mainnet",
     "url": "https://www.coindesk.com/defi/2026/origin-protocol",
     "summary": "Origin Protocol OGN token staking DeFi update"},
    {"title": "Origin Protocol Price Prediction: Is OGN a Good Investment?",
     "url": "https://example.com/ogn-analysis",
     "summary": "Origin Protocol OGN outlook"},
    {"title": "iExec 2025 Roadmap - Expanding the iExec and RLC Ecosystem",
     "url": "https://iex.ec/blog/iexec-2025-roadmap-and-rlc-ecosystem",
     "summary": "roadmap"},
    {"title": "iExec RLC climbs as decentralized computing demand grows",
     "url": "https://cointelegraph.com/news/iexec-rlc",
     "summary": "iExec RLC DePIN token rally"},
]


def test_ogn_pollution_dropped():
    for a in OGN_POLLUTION:
        assert not is_crypto_related(a, ticker="OGN",
                                     coin_name="Origin Protocol"), a["title"]


def test_rlc_pollution_dropped():
    for a in RLC_POLLUTION:
        assert not is_crypto_related(a, ticker="RLC",
                                     coin_name="iExec RLC"), a["title"]


def test_genuine_kept():
    assert is_crypto_related(GENUINE[0], ticker="OGN",
                             coin_name="Origin Protocol")
    assert is_crypto_related(GENUINE[1], ticker="OGN",
                             coin_name="Origin Protocol")
    # word-order-free name match: "iExec and RLC Ecosystem" covers
    # the mapped name "iExec RLC"
    assert is_crypto_related(GENUINE[2], ticker="RLC",
                             coin_name="iExec RLC")
    assert is_crypto_related(GENUINE[3], ticker="RLC",
                             coin_name="iExec RLC")


def test_bare_ticker_fallback_requires_crypto_context():
    # crypto-native content passes even for unmapped tickers
    ok = {"title": "HEI token listed on Binance futures",
          "url": "https://binance.com/en/futures/hei",
          "summary": "HEI coin launchpool crypto"}
    assert is_crypto_related(ok, ticker="HEI", coin_name="HEI")
    # equity page dropped
    bad = {"title": "Hei stock price today NYSE",
           "url": "https://finance.yahoo.com/quote/hei", "summary": ""}
    assert not is_crypto_related(bad, ticker="HEI", coin_name="HEI")
    # no crypto evidence at all dropped (prefer neutral over wrong)
    vague = {"title": "HEI annual report 2026",
             "url": "https://example.com/reports", "summary": "overview"}
    assert not is_crypto_related(vague, ticker="HEI", coin_name="HEI")


def test_mappings_present():
    assert BASE_TO_NAME["OGN"] == "Origin Protocol"
    assert BASE_TO_NAME["RLC"] == "iExec RLC"
    assert symbol_to_coin_name("OGNUSDT") == "Origin Protocol"
    assert symbol_to_coin_name("RLCUSDT") == "iExec RLC"


def test_news_query_crypto_qualified_for_unmapped():
    assert news_query_for("OGNUSDT") == "Origin Protocol news"
    assert news_query_for("RLCUSDT") == "iExec RLC news"
    # unmapped tickers must carry the crypto qualifier
    assert news_query_for("XYZUSDT") == "XYZ crypto news"
    assert "crypto" in news_query_for("HEIUSDT")


def test_sentiment_get_news_filters(monkeypatch):
    """sentiment._get_news applies the entity filter (unit, no network)."""
    import src.sentiment as S

    fake = {"results": OGN_POLLUTION + [GENUINE[0]]}
    monkeypatch.setattr(S, "tavily_search", lambda q, count=10: fake)
    sa = S.SentimentAnalyzer()
    news = sa._get_news("Origin Protocol", symbol="OGNUSDT")
    titles = [n["title"] for n in news]
    assert titles == [GENUINE[0]["title"]]
