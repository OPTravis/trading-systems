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
    "AVAX": "Avalanche",
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
    "FLOW": "Flow",
    "EGLD": "MultiversX",
    "XTZ": "Tezos",
    "XMR": "Monero",
    "ZEC": "Zcash",
    "DASH": "Dash",
    "NEO": "Neo",
    "KSM": "Kusama",
    "IOTA": "IOTA",
    "QNT": "Quant",
    "HOT": "Holo",
    "ZIL": "Zilliqa",
    "ONE": "Harmony",
    "ZEN": "Horizen",
    "THETA": "Theta Network",
    "XEM": "NEM",
    "WAVES": "Waves",
    "QTUM": "Qtum",
    # L2 / infra
    "ARB": "Arbitrum",
    "OP": "Optimism",
    "MATIC": "Polygon",
    "POL": "Polygon",
    "APT": "Aptos",
    "SUI": "Sui",
    "SEI": "Sei",
    "TIA": "Celestia",
    "INJ": "Injective",
    "RUNE": "THORChain",
    "FTM": "Fantom",
    "S": "Sonic",
    "STRK": "Starknet",
    "ZK": "zkSync",
    "MANTA": "Manta",
    "ZORA": "Zora",
    "STX": "Stacks",
    "IMX": "Immutable",
    "RNDR": "Render",
    "RENDER": "Render",
    "AR": "Arweave",
    "GRT": "The Graph",
    "ANKR": "Ankr",
    "CELR": "Celer Network",
    "SKL": "SKALE",
    "CTSI": "Cartesi",
    "AUDIO": "Audius",
    "ROSE": "Oasis Network",
    "KAVA": "Kava",
    "CELO": "Celo",
    "MINA": "Mina",
    "GNO": "Gnosis",
    "W": "Wormhole",
    "PYTH": "Pyth Network",
    "JUP": "Jupiter",
    "MOVE": "Movement",
    # DeFi
    "AAVE": "Aave",
    "LDO": "Lido DAO",
    "MKR": "Maker",
    "CRV": "Curve DAO",
    "CVX": "Convex Finance",
    "SNX": "Synthetix",
    "COMP": "Compound",
    "SUSHI": "SushiSwap",
    "1INCH": "1inch",
    "DYDX": "dYdX",
    "GMX": "GMX",
    "BLUR": "Blur",
    "PENDLE": "Pendle",
    "ENA": "Ethena",
    "JTO": "Jito",
    "KAMINO": "Kamino",
    "MET": "Metaplex",
    "EOS": "EOS",
    "CHZ": "Chiliz",
    "MASK": "Mask Network",
    "C98": "Coin98",
    "ALPACA": "Alpaca",
    "BEL": "Bella Protocol",
    # gaming / metaverse / NFT
    "AXS": "Axie Infinity",
    "SAND": "The Sandbox",
    "MANA": "Decentraland",
    "GALA": "Gala",
    "ENJ": "Enjin Coin",
    "APE": "ApeCoin",
    "PENGU": "Pudgy Penguins",
    "AAPT": "AAPT",
    "ALICE": "My Neighbor Alice",
    "TLM": "Alien Worlds",
    "PIXEL": "Pixels",
    "PORTAL": "Portal",
    "HIGH": "Highstreet",
    "MAGIC": "Treasure",
    "BEAM": "Beam",
    "GAS": "Gas",
    # memes
    "SHIB": "Shiba Inu",
    "PEPE": "Pepe",
    "BONK": "Bonk",
    "WIF": "dogwifhat",
    "FLOKI": "Floki",
    "ORDI": "ORDI",
    "TRUMP": "Official Trump",
    "VIRTUAL": "Virtuals Protocol",
    "AI16Z": "ai16z",
    "TURBO": "Turbo",
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
