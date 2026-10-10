"""Crypto-context entity filter for news results — WO-1040.

Structural root cause being fixed: news search queries built from a bare
ticker ("OGN news") are token-matched by search engines against ANY entity
sharing that token — Organon & Co. (NYSE: OGN), Oil & Gas News (OGN), the
Royal Logistic Corps (RLC), the Republican Liberty Caucus (RLC) — none of
which have anything to do with the crypto project. The junk filter in
market_researcher (`_looks_like_junk`) only catches aggregator/category
pages, not real articles about the WRONG entity.

This module answers one question: is this article about the CRYPTO
project behind this ticker? Two layers:

1. Positive evidence required — article must show crypto context
   (crypto vocabulary OR a crypto-native outlet domain OR the mapped
   project full name).
2. Hard exclusions — even with crypto vocabulary, strong equity-market
   signals (stock/NASDAQ/earnings/...) and known same-token entities
   (organon, oil & gas news, ...) reject the article.

Policy bias: prefer MISSING news (sentiment degrades to neutral) over
WRONG-entity news (sentiment becomes fabricated signal). Mapping-layer
only — scoring weights/thresholds untouched (WO-1040 constraint).
"""
from __future__ import annotations

from typing import Dict, Optional

# Crypto vocabulary: presence anywhere in title/summary = positive evidence.
# Kept broad (coin/token/defi/web3/...) but deliberately NOT including
# generic finance words (market, price, trading) which equity pages share.
_CRYPTO_POSITIVE = (
    "crypto", "cryptocurrency", "bitcoin", "ethereum", "blockchain",
    "defi", "web3", "altcoin", "stablecoin", "token", "coin", "airdrop",
    "wallet", "mainnet", "testnet", "staking", "launchpool", "perp",
    "dex", "cex", "binance", "coinbase", "bybit", "okx", "kraken",
    "coingecko", "coinmarketcap", "on-chain", "onchain", "hodl", "memecoin",
)

# Equity-market signals: even if crypto words appear, these make it an
# equity-markets article about a same-token stock.
_EQUITY_NEGATIVE = (
    "stock", "nasdaq", "nyse", "share price", "shares of", "earnings",
    "quarterly results", "dividend", "ipo", "shareholder", "sec filing",
    "10-k", "10-q", "wall street", "commission-free", "options flow",
)

# Known same-token entities observed polluting results (WO-1040 evidence:
# OGN/RLC live searches; extend as new collisions surface).
_KNOWN_WRONG_ENTITIES = (
    "organon",                    # NYSE: OGN pharma
    "oil & gas news", "oil and gas news", "ognnews.com",
    "royal logistic corps",       # RLC (British Army corps)
    "republican liberty caucus",  # RLC (US political org)
    "rlc residences",             # RLC (real-estate)
    "robinhood.com/stocks",       # equity page pattern
    "/stocks/",                   # equity quotes/overview URL pattern
    "/markets/stocks", "/quote/", "/quotes/",
)

# Crypto-native outlets: domain match = positive evidence on its own.
_CRYPTO_OUTLETS = (
    "coindesk.com", "cointelegraph.com", "decrypt.co", "theblock.co",
    "crypto.news", "cryptoslate.com", "bitcoinmagazine.com",
    "coinmarketcap.com", "coingecko.com", "cryptobriefing.com",
    "blockworks.co", "thedefiant.io", "cryptopotato.com",
    "news.bitcoin.com", "ambcrypto.com", "beincrypto.com", "u.today",
    "binance.com", "coinbase.com", "bybit.com", "okx.com",
)

_EQUITY_HOSTS = (
    "finance.yahoo.com", "robinhood.com", "stocktitan.net", "public.com",
    "cnbc.com", "cnn.com/markets", "marketwatch.com", "wsj.com",
    "seekingalpha.com", "fool.com", "investing.com", "benzinga.com/stock",
    "stocktwits.com", "tradingview.com", "zacks.com", "barchart.com",
)


def _haystack(article: Dict) -> str:
    return " ".join([
        (article.get("title") or ""),
        (article.get("summary") or ""),
        (article.get("description") or ""),
        (article.get("body") or ""),
        (article.get("source") or ""),
    ]).lower()


def _url(article: Dict) -> str:
    return (article.get("url") or "").lower()


def is_crypto_related(
    article: Dict,
    ticker: Optional[str] = None,
    coin_name: Optional[str] = None,
) -> bool:
    """True only when the article plausibly covers the crypto project.

    Args:
        article: dict with title/summary/description/body/url/source keys
                 (news results from Tavily / Jina / ddgs all fit).
        ticker:  bare base ticker, e.g. "OGN" (optional).
        coin_name: mapped human name, e.g. "Origin Protocol" (optional).
    """
    url = _url(article)
    text = _haystack(article)

    # --- hard exclusions first (cheap, decisive) ---
    for bad in _KNOWN_WRONG_ENTITIES:
        if bad in url or bad in text:
            return False
    for host in _EQUITY_HOSTS:
        if host in url:
            return False
    # equity vocabulary dominance: equity signal AND no explicit project
    # name -> wrong entity (crypto words like "coin" can appear in
    # "coinbase listed Organon options" style articles)
    has_equity = any(w in text for w in _EQUITY_NEGATIVE)
    full_name = (coin_name or "").strip().lower()
    if full_name and ticker and full_name != ticker.lower():
        # coin_name is a REAL mapped name (not the bare-ticker fallback):
        # the project name must appear (full phrase OR all its significant
        # words, order-free — "iExec and RLC ecosystem" matches "iExec
        # RLC"), and equity signals still reject.
        name_words = [w for w in full_name.split() if w not in ("the", "of")]
        name_hit = full_name in text or all(w in text for w in name_words)
        # mapped tickers rarely collide with equity tokens and the query
        # already carries the project name, so ALSO accept ticker hits
        # that carry explicit crypto evidence (word or outlet) — that
        # saves SEO-style titles like "Why is AXS going up? token rally"
        # while wrong entities (all caught above by hard exclusions /
        # equity signals, re-verified 2026-10-10) stay out.
        tk = ticker.lower()
        if not name_hit and tk:
            has_crypto = (
                any(w in text for w in _CRYPTO_POSITIVE)
                or any(d in url for d in _CRYPTO_OUTLETS)
            )
            return tk in text and has_crypto and not has_equity
        return name_hit and not has_equity
    # bare-ticker fallback query: require positive crypto evidence
    has_crypto = (
        any(w in text for w in _CRYPTO_POSITIVE)
        or any(d in url for d in _CRYPTO_OUTLETS)
    )
    if not has_crypto:
        return False
    # both equity and crypto words → ambiguous, reject (prefer missing
    # over wrong)
    if has_equity:
        return False
    # ticker must actually appear somewhere (else "crypto" article is
    # about a different coin entirely)
    tk = (ticker or "").lower()
    if tk and tk not in text and tk not in url:
        return False
    return True
