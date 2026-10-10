"""Map exchange tickers to human coin names for news search queries.

WO-1015: searching news with raw tickers (``AXS`` / ``AXSUSDT``) surfaces
price pages and aggregators (TradingView tickers, exchange pages, CoinDesk
front pages) instead of actual articles — the sentiment analyzer then parses
non-news content and reads neutral with LOW confidence. News search engines
match on project names, so ``Axie Infinity crypto news today`` beats
``AXS crypto latest news``.

Unknown tickers fall back to the stripped base (still far better than a
full pair symbol). The table covers top-cap coins plus everything the scan
pool has historically surfaced; extend it freely — misses degrade to the
base ticker, never to a pair symbol.
"""

from __future__ import annotations

_QUOTE_SUFFIXES = ("USDT", "FDUSD", "USDC", "BUSD", "TUSD", "USD")

# base ticker -> human coin / project name
BASE_TO_NAME: dict[str, str] = {
    # majors
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "BNB": "BNB",
    "SOL": "Solana",
    "XRP": "XRP",
    "ADA": "Cardano",
    "DOGE": "Dogecoin",
    "TRX": "TRON",
    "AVAX": "Avalanche crypto",  # WO-1016: bare name returns NHL Avalanche hockey news
    "DOT": "Polkadot",
    "LINK": "Chainlink",
    "TON": "Toncoin",
    "LTC": "Litecoin",
    "BCH": "Bitcoin Cash",
    "XLM": "Stellar",
    "NEAR": "NEAR Protocol",
    "ATOM": "Cosmos",
    "UNI": "Uniswap",
    "ICP": "Internet Computer",
    "ETC": "Ethereum Classic",
    "FIL": "Filecoin",
    "HBAR": "Hedera",
    "VET": "VeChain",
    "ALGO": "Algorand",
    "FLOW": "Flow blockchain",
    "EGLD": "MultiversX",
    "XTZ": "Tezos",
    "XMR": "Monero",
    "ZEC": "Zcash",
    "DASH": "Dash cryptocurrency",
    "NEO": "Neo crypto",
    "KSM": "Kusama",
    "IOTA": "IOTA",
    "QNT": "Quant crypto",
    "HOT": "Holo crypto",
    "ZIL": "Zilliqa",
    "ONE": "Harmony ONE crypto",
    "ZEN": "Horizen",
    # WO-1040: same-token entity collisions (equity/pharma/military/political
    # names hijacking bare-ticker news searches — live-search evidence
    # 2026-10-10: Organon/Oil&Gas for OGN, Royal Logistic Corps/Republican
    # Liberty Caucus for RLC)
    "OGN": "Origin Protocol",
    "RLC": "iExec RLC",
    # WO-1040 impact sweep: remaining unmapped tickers historically seen in
    # the scan pool (only verified names added — unverified ones stay out
    # and rely on the bare-ticker crypto-context filter instead)
    "CAKE": "PancakeSwap",
    "FET": "Artificial Superintelligence Alliance",
    "RAY": "Raydium",
    "TAO": "Bittensor",
    "ZRO": "ZKsync",
    "SAGA": "Saga blockchain",
    "LSK": "Lisk",
    "XAUT": "Tether Gold",
    "0G": "0G Labs",
    "BABY": "Babylon crypto",
    "THETA": "Theta Network",
    "XEM": "NEM",
    "WAVES": "Waves crypto",
    "QTUM": "Qtum",
    # L2 / infra
    "ARB": "Arbitrum",
    "OP": "Optimism crypto",
    "MATIC": "Polygon",
    "POL": "Polygon",
    "APT": "Aptos",
    "SUI": "Sui blockchain",
    "SEI": "Sei blockchain",
    "TIA": "Celestia",
    "INJ": "Injective",
    "RUNE": "THORChain",
    "FTM": "Fantom",
    "S": "Sonic blockchain",
    "STRK": "Starknet",
    "ZK": "zkSync",
    "MANTA": "Manta Network",
    "ZORA": "Zora",
    "STX": "Stacks",
    "IMX": "Immutable X",
    "RNDR": "Render crypto",
    "RENDER": "Render crypto",
    "AR": "Arweave",
    "GRT": "The Graph",
    "ANKR": "Ankr",
    "CELR": "Celer Network",
    "SKL": "SKALE",
    "CTSI": "Cartesi",
    "AUDIO": "Audius",
    "ROSE": "Oasis Network ROSE",
    "KAVA": "Kava",
    "CELO": "Celo",
    "MINA": "Mina",
    "GNO": "Gnosis",
    "W": "Wormhole",
    "WLD": "Worldcoin",  # WO-1016: bare ticker returns Australian local news
    "PYTH": "Pyth Network",
    "JUP": "Jupiter exchange",
    "MOVE": "Movement blockchain",
    # DeFi
    "AAVE": "Aave",
    "LDO": "Lido DAO",
    "MKR": "Maker",
    "CRV": "Curve DAO",
    "CVX": "Convex Finance",
    "SNX": "Synthetix",
    "COMP": "Compound finance",
    "SUSHI": "SushiSwap",
    "AERO": "Aerodrome Finance",  # WO-1016: bare ticker returns aviation news
    "1INCH": "1inch",
    "DYDX": "dYdX",
    "GMX": "GMX",
    "BLUR": "Blur crypto",
    "PENDLE": "Pendle finance",
    "ENA": "Ethena",
    "JTO": "Jito",
    "KAMINO": "Kamino",
    "MET": "Metaplex",
    "EOS": "EOS crypto",
    "CHZ": "Chiliz",
    "MASK": "Mask Network",
    "ORCA": "Orca crypto",  # WO-1016: bare ticker returns orca-whale news
    "NIL": "Nillion",  # WO-1017: bare ticker returns US college-sports NIL news
    "C98": "Coin98",
    "ALPACA": "Alpaca crypto",
    "BEL": "Bella Protocol",
    # gaming / metaverse / NFT
    "AXS": "Axie Infinity",
    "SAND": "The Sandbox",
    "MANA": "Decentraland",
    "GALA": "Gala Games",
    "ENJ": "Enjin Coin",
    "APE": "ApeCoin",
    "PENGU": "Pudgy Penguins",
    "AAPT": "AAPT",
    "ALICE": "My Neighbor Alice",
    "TLM": "Alien Worlds",
    "PIXEL": "Pixels",
    "PORTAL": "Portal crypto",
    "HIGH": "Highstreet",
    "MAGIC": "Treasure DAO",
    "BEAM": "Beam crypto",
    "GAS": "Neo GAS token",
    # memes
    "SHIB": "Shiba Inu",
    "PEPE": "Pepe",
    "BONK": "Bonk",
    "MUBARAK": "Mubarak meme coin",  # WO-1016: bare ticker returns President Mubarak obituaries
    "WIF": "dogwifhat",
    "FLOKI": "Floki",
    "ORDI": "ORDI",
    "TRUMP": "Official Trump",
    "VIRTUAL": "Virtuals Protocol",
    "AI16Z": "ai16z",
    "TURBO": "Turbo meme coin",
    "MEME": "Memecoin",
    "BABYDOGE": "Baby Doge Coin",
    # fan tokens
    "LAZIO": "Lazio",
    "PSG": "Paris Saint-Germain",
    "JUV": "Juventus",
    "CITY": "Manchester City",
    "ACM": "AC Milan",
    "BAR": "FC Barcelona",
    "AFA": "Argentina Fan Token",
}


def strip_quote_asset(symbol: str) -> str:
    """``AXSUSDT`` / ``AXS-USDT`` / ``AXS/USDT`` / ``AXS`` -> ``AXS``.

    Unknown suffixes are left untouched (e.g. ``AXSETH`` stays ``AXSETH``).
    """
    s = symbol.replace("-", "").replace("/", "").replace("_", "").upper()
    for q in _QUOTE_SUFFIXES:
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)]
    return s


def symbol_to_coin_name(symbol: str) -> str:
    """``AXSUSDT`` / ``AXS`` / ``axs`` -> ``Axie Infinity``; unknown -> base.

    The result is meant for news search queries (``f"{name} crypto news
    today"``), never for exchange API calls — those need the raw symbol.
    """
    base = strip_quote_asset(symbol.strip())
    return BASE_TO_NAME.get(base, base)


def news_query_for(symbol: str) -> str:
    """Best-effort news SEARCH QUERY for a symbol — WO-1040.

    Mapped tickers search on the human project name ("Origin Protocol
    news"); unmapped tickers degrade to the bare base but MUST carry an
    explicit crypto qualifier ("OGN crypto news") — bare-ticker queries
    are token-matched against same-name equity/military/political
    entities (WO-1040 live evidence). Result-side filtering still applies
    (news_entity_filter) — this only reduces pollution at the source.
    """
    base = strip_quote_asset(symbol.strip())
    name = BASE_TO_NAME.get(base)
    if name and name != base:
        return f"{name} news"
    return f"{base} crypto news"
