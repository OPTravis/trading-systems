"""WO-1015: news search queries must use human coin names, not tickers.

Ticker queries ("AXS+crypto+latest+news") surfaced price/aggregator pages
(TradingView, exchange pages, CoinDesk fronts) — sentiment parsed non-news
content and read neutral with LOW confidence. Fixes pinned here:
  1. queries use the human project name ("Axie Infinity news")
  2. Jina results are filtered for price/aggregator/verification junk
  3. thin results (<3 real articles) are topped up from DDGS's news
     vertical; a fully-junk result set fails open to the raw list
No network access: all HTTP layers are faked.
"""

import sys
import types

import pytest

from src.coin_names import BASE_TO_NAME, strip_quote_asset, symbol_to_coin_name
from src.market_researcher import MarketResearcher, _looks_like_junk


class TestCoinNameMapping:
    """Pure-function ticker -> coin name mapping."""

    def test_known_pairs_from_workorder(self):
        assert symbol_to_coin_name("AXSUSDT") == "Axie Infinity"
        assert symbol_to_coin_name("SOLUSDT") == "Solana"

    def test_known_base_ticker(self):
        assert symbol_to_coin_name("BTC") == "Bitcoin"
        assert symbol_to_coin_name("PENGUUSDT") == "Pudgy Penguins"

    def test_case_and_separator_insensitive(self):
        assert symbol_to_coin_name("axs") == "Axie Infinity"
        assert symbol_to_coin_name("AXS-USDT") == "Axie Infinity"
        assert symbol_to_coin_name("AXS/USDT") == "Axie Infinity"

    def test_unknown_degrades_to_base_not_pair(self):
        # miss in table -> stripped base, never the full pair symbol
        assert symbol_to_coin_name("XYZUSDT") == "XYZ"
        assert symbol_to_coin_name("NEWCOIN") == "NEWCOIN"

    def test_non_quote_suffix_untouched(self):
        assert strip_quote_asset("AXSETH") == "AXSETH"

    def test_wo1016_collision_coins_disambiguated(self):
        # WO-1016: observed collisions (Mubarak obituaries, Aero aviation
        # news, Avalanche NHL hockey, Sui category pages, orca whales,
        # WLD Australian local news)
        assert symbol_to_coin_name("MUBARAKUSDT") == "Mubarak meme coin"
        assert symbol_to_coin_name("AEROUSDT") == "Aerodrome Finance"
        assert symbol_to_coin_name("AVAXUSDT") == "Avalanche crypto"
        assert symbol_to_coin_name("SUIUSDT") == "Sui blockchain"
        assert symbol_to_coin_name("ORCAUSDT") == "Orca crypto"
        assert symbol_to_coin_name("WLDUSDT") == "Worldcoin"
        # LTC/ETH keep clean names; their residue (403 page, CoinDesk
        # category page, official promo page) is handled by the junk filter

    def test_wo1016_preventive_disambiguation(self):
        # names that collide with non-crypto news topics (games, animals,
        # sports teams, pop culture, generic words)
        assert symbol_to_coin_name("SUSDT") == "Sonic blockchain"
        assert symbol_to_coin_name("GASUSDT") == "Neo GAS token"
        assert symbol_to_coin_name("ROSEUSDT") == "Oasis Network ROSE"
        assert symbol_to_coin_name("JUPUSDT") == "Jupiter exchange"
        assert symbol_to_coin_name("EOSUSDT") == "EOS crypto"
        assert symbol_to_coin_name("ALPACAUSDT") == "Alpaca crypto"
        assert symbol_to_coin_name("PENDLEUSDT") == "Pendle finance"

    def test_wo1016_fan_tokens_keep_club_names(self):
        # club news IS the real sentiment driver for fan tokens — not a
        # collision (unlike Avalanche/NHL), so these must stay untouched
        assert symbol_to_coin_name("PSGUSDT") == "Paris Saint-Germain"
        assert symbol_to_coin_name("CITYUSDT") == "Manchester City"

    def test_table_entries_are_clean(self):
        for base, name in BASE_TO_NAME.items():
            assert base == base.upper(), f"key not upper: {base}"
            assert name and not name.startswith(" "), f"bad name for {base}"
            assert "  " not in name


class TestJunkFilter:
    """Observed junk titles must be dropped; real headlines must survive."""

    @pytest.mark.parametrize(
        "title",
        [
            "CoinDesk: Bitcoin, Ethereum, XRP, Crypto News and Price Data",
            "Axie Infinity Price: AXS/USD Live Price Chart, Market Cap & News Today | CoinGecko",
            "Buy and sell Axie Infinity (AXS) and 20+ other crypto on Robinhood",
            "Bitcoin News Today: Latest BTC News & Live Updates | Bitcoin.com",
            "Latest Axie Infinity News - (AXS) Future Outlook, Trends & Market Insights",
            "Latest Axie Infinity News | crypto.news",
            "Axie Infinity (@AxieInfinity) on X",
            "Human Verification",
            "Just a moment...",
            "The Block",
            "reuters.com",
            "",
        ],
    )
    def test_junk_titles_dropped(self, title):
        assert _looks_like_junk({"title": title, "url": "https://x.example/1"})

    @pytest.mark.parametrize(
        "title",
        [
            "Axie Infinity token jumps 123% as game devs push major rewards change",
            "Project Harmonia Brings Institutional Tokenized Funds to Solana",
            "Why bitcoin may be at an inflection point",
            "Analyst warns Bitcoin is flashing a 2023 warning sign",
            "The Lunacian | Axie Infinity | Substack",
        ],
    )
    def test_real_articles_survive(self, title):
        assert not _looks_like_junk({"title": title, "url": "https://x.example/1"})

    def test_youtube_host_dropped_even_with_good_title(self):
        assert _looks_like_junk(
            {"title": "Bitcoin just did something it hasn't done in 45 weeks",
             "url": "https://www.youtube.com/watch?v=x"}
        )

    @pytest.mark.parametrize(
        "title",
        [
            "403 - Operations too frequent",
            "Ethereum (ETH) News | CoinDesk",
            "Sui | CoinDesk",
            "Solana (SOL) News Today | CoinDesk",
            "Litecoin - Buy, Hold, Pay & Learn",
            "Litecoin News",
            "Avalanche Crypto News (AVAX)",
            "Aerodrome Finance USD (AERO29270-USD) Latest Stock News & Headlines - Yahoo Finance",
            "Aerodrome Finance Trade Ideas — CRYPTO:AERODUSD — TradingView",
            "Aerodrome Finance (AERO) Price Prediction 2026, 2027-2030",
            "Litecoin USD (LTC-USD) Latest Stock News & Headlines - Yahoo Finance",
        ],
    )
    def test_wo1016_error_and_category_pages_dropped(self, title):
        # bare rules work without coin context...
        if title in ("403 - Operations too frequent", "Litecoin News"):
            pytest.skip("needs coin_name context — covered below")
        assert _looks_like_junk({"title": title, "url": "https://x.example/1"})

    def test_wo1016_error_page_dropped(self):
        assert _looks_like_junk(
            {"title": "403 - Operations too frequent", "url": "https://x/1"}
        )

    @pytest.mark.parametrize(
        "title",
        [
            # WO-1017 evidence: category / official-site / blog-index shapes
            "Cardano (ADA) News Headlines | Nasdaq",
            "Cardano, Secure Decentralized Blockchain Platform",
            "Blog | Ondo Finance",          # blog prefix
            "NEAR Protocol Blog | Web3 Insights",  # blog infix
            "The Sui Blog",                  # blog trailing (old rule too)
            "Home | Nillion",
            "Cardano: Latest News, Social Media Updates and Insights | CryptoRank.io",
            "Ondo (ONDO) Price Forecasts, Predictions & News",
        ],
    )
    def test_wo1017_residual_shapes_dropped(self, title):
        assert _looks_like_junk({"title": title, "url": "https://x/1"})

    def test_wo1017_blog_rule_keeps_non_blog_articles(self):
        assert not _looks_like_junk(
            {"title": "Ondo Finance expands tokenized treasuries to new chains",
             "url": "https://x/1"}
        )

    def test_wo1017_nil_mapped_to_nillion(self):
        # US college-sports NIL (Name/Image/Likeness) collision
        assert symbol_to_coin_name("NILUSDT") == "Nillion"

    def test_wo1016_category_page_needs_coin_context(self):
        # "Litecoin News" alone is a bare outlet category page
        assert _looks_like_junk(
            {"title": "Litecoin News", "url": "https://x/1"}, "Litecoin"
        )
        assert _looks_like_junk(
            {"title": "Sui News Today", "url": "https://x/1"}, "Sui blockchain"
        )
        assert _looks_like_junk(
            {"title": "Ethereum", "url": "https://x/1"}, "Ethereum"
        )
        assert _looks_like_junk(
            {"title": "The Sui Blog", "url": "https://x/1"}, "Sui blockchain"
        )
        # without coin context the plain title is too generic to judge
        assert not _looks_like_junk(
            {"title": "Litecoin News", "url": "https://x/1"}
        )

    def test_wo1016_real_articles_with_coin_context_survive(self):
        # the exact 5 protected samples from the workorder constraint
        assert not _looks_like_junk(
            {"title": "Axie Infinity token jumps 123% as game devs push major rewards change",
             "url": "https://x/1"}, "Axie Infinity"
        )
        assert not _looks_like_junk(
            {"title": "Project Harmonia Brings Institutional Tokenized Funds to Solana",
             "url": "https://x/1"}, "Solana"
        )
        assert not _looks_like_junk(
            {"title": "Why bitcoin may be at an inflection point",
             "url": "https://x/1"}, "Bitcoin"
        )
        assert not _looks_like_junk(
            {"title": "Analyst warns Bitcoin is flashing a 2023 warning sign",
             "url": "https://x/1"}, "Bitcoin"
        )
        assert not _looks_like_junk(
            {"title": "The Lunacian | Axie Infinity | Substack",
             "url": "https://x/1"}, "Axie Infinity"
        )


def _fake_jina_response(rows):
    class FakeResp:
        def json(self):
            return {"data": rows}
    return FakeResp()


def _jina_row(title, url="https://news.example/article"):
    return {"title": title, "description": f"{title} — body text", "url": url}


class TestJinaQueryUsesCoinName:
    """_research_news must hit s.jina.ai with a human-name query."""

    def test_url_uses_quoted_coin_name(self, monkeypatch):
        import src.market_researcher as mr

        monkeypatch.setenv("JINA_API_KEY", "test-key")
        captured = {}
        monkeypatch.setattr(
            mr._jina_session, "get",
            lambda url, **kw: (captured.__setitem__("url", url), _fake_jina_response([]))[1],
        )
        monkeypatch.setattr(
            MarketResearcher, "_ddgs_news_supplement",
            lambda self, name, need: [],  # supplement also empty
        )

        articles = mr.MarketResearcher()._research_news("AXSUSDT")

        assert articles == []  # empty jina payload, empty supplement -> empty list
        assert captured["url"] == "https://s.jina.ai/Axie+Infinity+news"
        assert "AXSUSDT" not in captured["url"]
        assert "latest" not in captured["url"]

    def test_base_ticker_input_also_mapped(self, monkeypatch):
        import src.market_researcher as mr

        monkeypatch.setenv("JINA_API_KEY", "test-key")
        captured = {}
        monkeypatch.setattr(
            mr._jina_session, "get",
            lambda url, **kw: (captured.__setitem__("url", url), _fake_jina_response([]))[1],
        )
        mr.MarketResearcher()._research_news("SOL")
        assert captured["url"] == "https://s.jina.ai/Solana+news"


class TestJunkFilteringAndTopUp:
    """10 pulled -> junk dropped -> <3 topped up from DDGS news -> fail-open."""

    def _patch_llm_sentiment(self, monkeypatch):
        # identity batch: no LLM in unit tests
        monkeypatch.setattr(
            MarketResearcher, "_batch_llm_sentiment",
            lambda self, arts: [0.0] * len(arts),
        )

    def test_junk_dropped_from_result(self, monkeypatch):
        import src.market_researcher as mr

        monkeypatch.setenv("JINA_API_KEY", "k")
        rows = [
            _jina_row("Axie Infinity Price: AXS/USD Live Price Chart | CoinGecko"),
            _jina_row("Axie Infinity token jumps 123% as game devs push rewards change"),
            _jina_row("Human Verification"),
            # WO-1040: realistic crypto wording (entity filter needs either
            # the project name or explicit crypto evidence in title/body)
            _jina_row("Why Is the AXS token Going Up in 2026? The Real Reasons"),
            _jina_row("Axie Infinity partners with a major game studio"),
        ]
        monkeypatch.setattr(mr._jina_session, "get", lambda url, **kw: _fake_jina_response(rows))
        self._patch_llm_sentiment(monkeypatch)
        monkeypatch.setattr(
            MarketResearcher, "_ddgs_news_supplement",
            lambda self, name, need: pytest.fail("3 real articles: no top-up expected"),
        )

        articles = mr.MarketResearcher()._research_news("AXSUSDT")

        titles = [a["title"] for a in articles]
        assert titles == [
            "Axie Infinity token jumps 123% as game devs push rewards change",
            "Why Is the AXS token Going Up in 2026? The Real Reasons",
            "Axie Infinity partners with a major game studio",
        ]

    def test_thin_result_topped_up_from_ddgs_news(self, monkeypatch):
        import src.market_researcher as mr

        monkeypatch.setenv("JINA_API_KEY", "k")
        rows = [_jina_row("Axie Infinity token jumps 123% as game devs push rewards change")]
        monkeypatch.setattr(mr._jina_session, "get", lambda url, **kw: _fake_jina_response(rows))
        self._patch_llm_sentiment(monkeypatch)

        supplement_calls = []

        def fake_top_up(self, name, need):
            supplement_calls.append((name, need))
            return [
                {"title": "DDGS real article about Axie", "summary": "s",
                 "sentiment": 0.0, "source": "TheStreet", "url": "https://x/2"}
                for _ in range(need)
            ]

        monkeypatch.setattr(MarketResearcher, "_ddgs_news_supplement", fake_top_up)

        articles = mr.MarketResearcher()._research_news("AXSUSDT")

        # WO-1040: supplement now takes the raw symbol (it derives its own
        # crypto-qualified query via news_query_for)
        assert supplement_calls == [("AXSUSDT", 4)]
        assert len(articles) == 5
        assert sum(1 for a in articles if a["title"].startswith("DDGS")) == 4

    def test_all_junk_failopens_to_raw(self, monkeypatch):
        import src.market_researcher as mr

        monkeypatch.setenv("JINA_API_KEY", "k")
        rows = [
            _jina_row("CoinDesk: Bitcoin Price Data"),
            _jina_row("The Block"),
        ]
        monkeypatch.setattr(mr._jina_session, "get", lambda url, **kw: _fake_jina_response(rows))
        self._patch_llm_sentiment(monkeypatch)
        monkeypatch.setattr(
            MarketResearcher, "_ddgs_news_supplement", lambda self, name, need: []
        )

        articles = mr.MarketResearcher()._research_news("BTCUSDT")

        # fail-open: unfiltered beats empty
        assert len(articles) == 2


class TestDDGSFallbackUsesNewsVertical:
    """Fallback must use ddgs.news (articles), not ddgs.text (aggregators)."""

    def test_news_api_and_fields(self, monkeypatch):
        import src.market_researcher as mr

        captured = {}

        class FakeDDGS:
            def text(self, *a, **kw):
                pytest.fail("text() must not be used for news")

            def news(self, query, max_results=5):
                captured["query"] = query
                captured["max_results"] = max_results
                return [
                    {"title": "Axie Infinity t", "body": "b", "source": "Ex",
                     # WO-1040: neutral host — cnbc.com sits on the equity
                     # blocklist and would be entity-filtered
                     "url": "https://news.example/x", "date": "2026-10-04"},
                ]

        fake_mod = types.ModuleType("ddgs")
        fake_mod.DDGS = FakeDDGS
        monkeypatch.setitem(sys.modules, "ddgs", fake_mod)

        articles = mr.MarketResearcher()._research_news_ddgs("AXSUSDT")

        # WO-1040: news_query_for appends the " news" qualifier
        assert captured["query"] == "Axie Infinity news"
        assert articles == [
            {"title": "Axie Infinity t", "summary": "b", "sentiment": 0.0,
             "source": "Ex", "url": "https://news.example/x"}
        ]


class TestTavilyQueryUsesCoinName:
    """SentimentAnalyzer._get_news must search on the coin name too."""

    def test_analyze_coin_queries_coin_name(self, monkeypatch):
        import src.sentiment as sent

        captured = {}

        def fake_tavily(query, count=10):
            captured["query"] = query
            return {"results": []}

        monkeypatch.setattr(sent, "tavily_search", fake_tavily)

        result = sent.SentimentAnalyzer().analyze_coin("AXSUSDT")

        assert captured["query"] == "Axie Infinity cryptocurrency news today"
        assert result["coin_name"] == "Axie Infinity"
