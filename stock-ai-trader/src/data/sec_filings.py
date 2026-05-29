"""
SEC EDGAR filing retrieval and parsing (10-K, 10-Q, 8-K).
"""
import logging
import re
from typing import Optional

import requests

logger = logging.getLogger(__name__)

EDGAR_BASE = "https://efts.sec.gov/LATEST/search-index"
EDGAR_FULL_TEXT = "https://efts.sec.gov/LATEST/search-index"
EDGAR_SUBMISSIONS = "https://data.sec.gov/submissions"
EDGAR_USER_AGENT = "stock-ai-trader research@example.com"


class SECFilings:
    """
    SEC EDGAR filing retrieval and text extraction.
    Supports 10-K (annual), 10-Q (quarterly), and 8-K (current events) filings.
    """

    def __init__(self, user_agent: Optional[str] = None):
        self.user_agent = user_agent or EDGAR_USER_AGENT
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.user_agent})
        self._cache: dict = {}

    def _get_company_cik(self, symbol: str) -> Optional[str]:
        """Resolve ticker symbol to CIK number."""
        cache_key = f"cik|{symbol}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            # Use the company tickers JSON from SEC
            resp = self.session.get(
                "https://www.sec.gov/files/company_tickers.json",
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            for _, entry in data.items():
                if entry.get("ticker", "").upper() == symbol.upper():
                    cik = str(entry["cik_str"]).zfill(10)
                    self._cache[cache_key] = cik
                    return cik
        except Exception as exc:
            logger.error("Failed to resolve CIK for %s: %s", symbol, exc)

        return None

    def get_latest_filing(self, symbol: str, filing_type: str = "10-K") -> Optional[dict]:
        """
        Get the latest filing of a given type for a symbol.

        Args:
            symbol: Ticker symbol.
            filing_type: '10-K', '10-Q', or '8-K'.

        Returns:
            Dict with keys: filing_type, date, url, accession_number, description.
            None if not found.
        """
        cache_key = f"filing|{symbol}|{filing_type}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        cik = self._get_company_cik(symbol)
        if not cik:
            logger.warning("Could not resolve CIK for %s", symbol)
            return None

        logger.info("Fetching latest %s filing for %s (CIK: %s)", filing_type, symbol, cik)

        try:
            resp = self.session.get(
                f"{EDGAR_SUBMISSIONS}/CIK{cik}.json",
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            recent = data.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            dates = recent.get("filingDate", [])
            accessions = recent.get("accessionNumber", [])
            descriptions = recent.get("primaryDocDescription", [])
            primary_docs = recent.get("primaryDocument", [])

            for i, form in enumerate(forms):
                if form == filing_type or (filing_type == "10-K" and form == "10-K/A"):
                    acc_no = accessions[i].replace("-", "")
                    doc_url = (
                        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                        f"{acc_no}/{primary_docs[i]}"
                    )
                    result = {
                        "filing_type": filing_type,
                        "form": form,
                        "date": dates[i],
                        "url": doc_url,
                        "accession_number": accessions[i],
                        "description": descriptions[i] if i < len(descriptions) else "",
                        "cik": cik,
                        "symbol": symbol,
                    }
                    self._cache[cache_key] = result
                    return result

        except Exception as exc:
            logger.error("Failed to fetch filing for %s: %s", symbol, exc)

        return None

    def parse_filing(self, url: str) -> str:
        """
        Download and extract text content from an SEC filing URL.

        Args:
            url: Direct URL to the filing document.

        Returns:
            Extracted plain text (HTML tags stripped).
        """
        cache_key = f"parsed|{url}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        logger.info("Parsing filing: %s", url)
        try:
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
            text = resp.text

            # Strip HTML tags
            text = re.sub(r"<[^>]+>", " ", text)
            # Normalize whitespace
            text = re.sub(r"\s+", " ", text).strip()
            # Remove excessive newlines
            text = re.sub(r"\n{3,}", "\n\n", text)

            # Limit size to avoid memory issues (keep first 100K chars)
            if len(text) > 100_000:
                text = text[:100_000] + "\n... [TRUNCATED]"

            self._cache[cache_key] = text
            return text

        except Exception as exc:
            logger.error("Failed to parse filing at %s: %s", url, exc)
            return ""

    def get_filings(self, symbol: str, filing_type: str = "10-K", limit: int = 5) -> list[dict]:
        """
        Get multiple recent filings of a given type.

        Returns:
            List of filing dicts (same structure as get_latest_filing).
        """
        cik = self._get_company_cik(symbol)
        if not cik:
            return []

        try:
            resp = self.session.get(f"{EDGAR_SUBMISSIONS}/CIK{cik}.json", timeout=15)
            resp.raise_for_status()
            data = resp.json()

            recent = data.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            dates = recent.get("filingDate", [])
            accessions = recent.get("accessionNumber", [])
            descriptions = recent.get("primaryDocDescription", [])
            primary_docs = recent.get("primaryDocument", [])

            filings = []
            for i, form in enumerate(forms):
                if form == filing_type:
                    acc_no = accessions[i].replace("-", "")
                    doc_url = (
                        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                        f"{acc_no}/{primary_docs[i]}"
                    )
                    filings.append({
                        "filing_type": filing_type,
                        "form": form,
                        "date": dates[i],
                        "url": doc_url,
                        "accession_number": accessions[i],
                        "description": descriptions[i] if i < len(descriptions) else "",
                    })
                    if len(filings) >= limit:
                        break

            return filings

        except Exception as exc:
            logger.error("Failed to fetch filings for %s: %s", symbol, exc)
            return []
