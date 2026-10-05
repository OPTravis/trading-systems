"""WO-1017: on-chain TVL dimension needs a cross-round fallback cache.

Callers (dimension_scorer, price_predictor) instantiate DeFiLlamaOnChain
fresh every scan round, so the old instance-scoped memory cache could never
carry a value across rounds — 7 consecutive empty rounds on 10/5 hit that
hole. Last-known-good values now persist to disk (per-chain, with ts) and
are served when an entire fetch round comes back empty, for up to 2h.
No network access: requests.get is monkeypatched.
"""

import json
import time

import pytest

from src.data_feed_onchain import DeFiLlamaOnChain, FALLBACK_TTL


@pytest.fixture
def cache_file(tmp_path, monkeypatch):
    f = tmp_path / "onchain_tvl_cache.json"
    monkeypatch.setenv("ONCHAIN_TVL_CACHE_PATH", str(f))
    # re-import-level constant is read at import time; patch it too
    import src.data_feed_onchain as mod
    monkeypatch.setattr(mod, "CACHE_FILE", str(f))
    return f


def _make_failing_fetch(monkeypatch):
    def fake_get(url, timeout=10, **kw):
        raise ConnectionError("simulated outage")

    monkeypatch.setattr("src.data_feed_onchain.requests.get", fake_get)


def _make_ok_fetch(monkeypatch, value=1.5):
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"tvl": 100}, {"tvl": 100 * (1 + value / 100)}]

    monkeypatch.setattr(
        "src.data_feed_onchain.requests.get",
        lambda url, timeout=10, **kw: FakeResp(),
    )


class TestFallbackServing:
    def test_empty_round_serves_fresh_disk_cache(self, cache_file, monkeypatch):
        # a previous round persisted 2 fresh chains
        now = time.time()
        cache_file.write_text(json.dumps({
            "chains": {
                "Ethereum": {"chg": 2.0, "ts": now - 60},
                "Solana": {"chg": -1.0, "ts": now - 60},
                "BSC": {"chg": 0.5, "ts": now - FALLBACK_TTL - 10},  # too old
            }
        }))
        _make_failing_fetch(monkeypatch)

        changes = DeFiLlamaOnChain().get_chain_tvl_changes()

        assert changes == {"Ethereum": 2.0, "Solana": -1.0}

    def test_empty_round_beyond_ttl_goes_empty(self, cache_file, monkeypatch):
        now = time.time()
        cache_file.write_text(json.dumps({
            "chains": {"Ethereum": {"chg": 2.0, "ts": now - FALLBACK_TTL - 1}}
        }))
        _make_failing_fetch(monkeypatch)

        assert DeFiLlamaOnChain().get_chain_tvl_changes() == {}

    def test_memory_cache_from_same_process_also_serves(self, cache_file, monkeypatch):
        _make_ok_fetch(monkeypatch, value=3.0)
        feed = DeFiLlamaOnChain()
        first = feed.get_chain_tvl_changes()
        assert len(first) == 7  # all chains OK

        # now the network dies mid-process: same instance must fall back
        _make_failing_fetch(monkeypatch)
        second = feed.get_chain_tvl_changes()
        assert second == first

    def test_new_process_after_reboot_serves_disk_cache(self, cache_file, monkeypatch):
        _make_ok_fetch(monkeypatch, value=1.0)
        feed = DeFiLlamaOnChain()
        first = feed.get_chain_tvl_changes()

        # simulate a fresh process (new instance) plus total outage
        _make_failing_fetch(monkeypatch)
        second = DeFiLlamaOnChain().get_chain_tvl_changes()
        assert second == first


class TestPersistence:
    def test_success_persists_to_disk(self, cache_file, monkeypatch):
        _make_ok_fetch(monkeypatch, value=2.0)
        DeFiLlamaOnChain().get_chain_tvl_changes()

        data = json.loads(cache_file.read_text())
        chains = data["chains"]
        assert set(chains) == set(DeFiLlamaOnChain.MAJOR_CHAINS)
        assert all(abs(v["chg"] - 2.0) < 1e-9 for v in chains.values())

    def test_partial_round_refreshes_own_chains_only(self, cache_file, monkeypatch):
        # old cache: Ethereum very fresh; Solana stale beyond TTL
        now = time.time()
        cache_file.write_text(json.dumps({
            "chains": {
                "Ethereum": {"chg": 9.9, "ts": now - 30},
                "Solana": {"chg": 8.8, "ts": now - FALLBACK_TTL - 60},
            }
        }))

        # this round: everything fails EXCEPT Ethereum succeeds with 1.0%
        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return [{"tvl": 100}, {"tvl": 101}]

        def mixed_get(url, timeout=10, **kw):
            if "Ethereum" in url:
                return FakeResp()
            raise ConnectionError("simulated outage")

        monkeypatch.setattr("src.data_feed_onchain.requests.get", mixed_get)

        changes = DeFiLlamaOnChain().get_chain_tvl_changes()
        # WO-1010 semantics preserved: partial round returns only itself,
        # no stale backfill of the failing chains
        assert changes == {"Ethereum": 1.0}

        # but the store refreshed Ethereum; Solana stays untouched/stale
        data = json.loads(cache_file.read_text())
        assert data["chains"]["Ethereum"]["chg"] == 1.0
        assert data["chains"]["Solana"]["chg"] == 8.8

        # a later fully-empty round now serves only Ethereum (Solana > TTL)
        _make_failing_fetch(monkeypatch)
        assert DeFiLlamaOnChain().get_chain_tvl_changes() == {"Ethereum": 1.0}

    def test_corrupt_cache_file_ignored(self, cache_file, monkeypatch):
        cache_file.write_text("{not json")
        _make_failing_fetch(monkeypatch)
        assert DeFiLlamaOnChain().get_chain_tvl_changes() == {}


class TestOnchainScore:
    def test_score_uses_fallback_data(self, cache_file, monkeypatch):
        now = time.time()
        cache_file.write_text(json.dumps({
            "chains": {"Ethereum": {"chg": 4.0, "ts": now - 60}}
        }))
        _make_failing_fetch(monkeypatch)

        score = DeFiLlamaOnChain().get_onchain_score()
        # avg 4.0% -> 50 + 4*5 = 70
        assert score == pytest.approx(70.0)
