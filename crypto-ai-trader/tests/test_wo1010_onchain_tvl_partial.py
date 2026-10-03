"""WO-1010: single-chain TVL failure must degrade, not discard the dimension.

Repro base: 10/2 04:00 - 10/3 Base chain requests stalled past the
as_completed timeout; the TimeoutError escaped with the partial results
discarded, leaving onchain(25%) empty for hours while 6/7 chains had data.
"""
import concurrent.futures
import logging
import sys
from types import SimpleNamespace

sys.path.insert(0, ".")

import src.data_feed_onchain as dfo
import src.data_feed_llama as dfl
from src.data_feed_onchain import DeFiLlamaOnChain
from src.data_feed_llama import LlamaDataFeed
from src.dimension_scorer import DimensionScorer


CHAINS = list(DeFiLlamaOnChain.MAJOR_CHAINS)  # 7 chains


def _fake_as_completed_factory(n_ok):
    """as_completed stub: yield first n_ok futures, then raise pool TimeoutError."""
    def fake(futures, timeout=None):
        done = 0
        for f in futures:
            if done >= n_ok:
                break
            done += 1
            yield f
        raise TimeoutError(f"{len(futures) - n_ok} (of {len(futures)}) futures unfinished")
    return fake


class TestOnchainPoolTimeoutDegrades:
    """DeFiLlamaOnChain.get_chain_tvl_changes keeps partial results."""

    def test_timeout_keeps_finished_chains_and_warns(self, monkeypatch, caplog):
        feed = DeFiLlamaOnChain()
        per_chain = {c: (1.0 if c == "Ethereum" else -0.5) for c in CHAINS}
        monkeypatch.setattr(
            feed, "_fetch_chain_tvl_change",
            lambda chain: per_chain[chain], raising=True)
        # 4 of 7 resolve, pool then times out (the 10/3 Base scenario)
        monkeypatch.setattr(
            dfo, "as_completed", _fake_as_completed_factory(4))

        with caplog.at_level(logging.WARNING, logger="src.data_feed_onchain"):
            out = feed.get_chain_tvl_changes()

        assert isinstance(out, dict)
        assert len(out) == 4
        assert out["Ethereum"] == 1.0
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "partial" in msgs and "degraded" in msgs

    def test_timeout_warning_names_missing_chains(self, monkeypatch, caplog):
        feed = DeFiLlamaOnChain()
        monkeypatch.setattr(
            feed, "_fetch_chain_tvl_change", lambda chain: 2.0)
        monkeypatch.setattr(dfo, "as_completed", _fake_as_completed_factory(6))

        with caplog.at_level(logging.WARNING, logger="src.data_feed_onchain"):
            feed.get_chain_tvl_changes()

        msgs = " ".join(r.getMessage() for r in caplog.records)
        # the stalled chain must be named, not silently dropped
        assert "6/7" in msgs
        missing = [c for c in CHAINS if c not in msgs]
        assert len(missing) == 6  # warning names only the stalled chain

    def test_all_chains_success_unchanged(self, monkeypatch, caplog):
        feed = DeFiLlamaOnChain()
        monkeypatch.setattr(
            feed, "_fetch_chain_tvl_change", lambda chain: 0.75)
        # real as_completed: no patch, everything resolves instantly

        with caplog.at_level(logging.WARNING, logger="src.data_feed_onchain"):
            out = feed.get_chain_tvl_changes()

        assert len(out) == 7
        assert all(v == 0.75 for v in out.values())
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "degraded" not in msgs and "partial" not in msgs

    def test_failed_chain_returns_none_degrades_with_warning(self, monkeypatch, caplog):
        feed = DeFiLlamaOnChain()

        def fetch(chain):
            return None if chain == "Base" else 0.4

        monkeypatch.setattr(feed, "_fetch_chain_tvl_change", fetch)

        with caplog.at_level(logging.WARNING, logger="src.data_feed_onchain"):
            out = feed.get_chain_tvl_changes()

        assert len(out) == 6
        assert "Base" not in out
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "Base" in msgs and "degraded" in msgs

    def test_all_fail_returns_empty_dict(self, monkeypatch):
        feed = DeFiLlamaOnChain()
        monkeypatch.setattr(
            feed, "_fetch_chain_tvl_change", lambda chain: None)
        out = feed.get_chain_tvl_changes()
        assert out == {}


class TestLlamaFeedPartialKeep:
    """LlamaDataFeed.get_chain_tvl keeps partial results on pool error."""

    def _bare_feed(self, monkeypatch):
        feed = object.__new__(LlamaDataFeed)
        feed._cache = {}
        feed._cache_ts = {}
        feed._cli_available = True
        monkeypatch.setattr(
            feed, "_call_llama",
            lambda op, params=None: [{"tvl": 100.0}, {"tvl": 105.0}])
        return feed

    def test_pool_error_keeps_partial_and_warns(self, monkeypatch, caplog):
        feed = self._bare_feed(monkeypatch)
        # as_completed is imported INSIDE get_chain_tvl — patch the stdlib attr
        monkeypatch.setattr(
            "concurrent.futures.as_completed",
            _fake_as_completed_factory(3))

        with caplog.at_level(logging.WARNING, logger="src.data_feed_llama"):
            out = feed.get_chain_tvl()

        assert out is not None and len(out) == 3
        assert all(v == 5.0 for v in out.values())  # (105-100)/100*100
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "degraded" in msgs and "3/5" in msgs

    def test_total_failure_still_none(self, monkeypatch):
        feed = self._bare_feed(monkeypatch)
        # every chain's data is unusable -> all Nones -> empty result
        monkeypatch.setattr(
            feed, "_call_llama", lambda op, params=None: None)
        monkeypatch.setattr(
            "concurrent.futures.as_completed",
            _fake_as_completed_factory(5))
        out = feed.get_chain_tvl()
        assert out is None


class TestScorerPartialVisibility:
    """_score_onchain surfaces partial fallback TVL in its signals."""

    def _patch_feeds(self, monkeypatch, changes):
        class _FakeOnchain:
            def get_chain_tvl_changes(self):
                return changes

        class _FakeLlama:
            def get_chain_tvl(self):
                return None  # force the fallback path

        monkeypatch.setattr(
            "src.data_feed_onchain.DeFiLlamaOnChain", _FakeOnchain)
        import src.dimension_scorer as ds
        monkeypatch.setattr(ds, "LlamaDataFeed", _FakeLlama, raising=False)
        return DimensionScorer(binance_client=SimpleNamespace(
            get_24hr_stats=lambda sym: None))

    def test_fallback_partial_signal_appended(self, monkeypatch):
        changes = {c: 0.3 for c in CHAINS if c != "Base"}  # 6 of 7
        scorer = self._patch_feeds(monkeypatch, changes)
        out = scorer._score_onchain()
        assert "tvl_fallback_partial_6/7" in out["signals"]
        assert out["data"]["chain_tvl_changes_fallback"]

    def test_full_fallback_no_partial_marker(self, monkeypatch):
        changes = {c: -2.0 for c in CHAINS}  # all 7, beyond +/-1 threshold
        scorer = self._patch_feeds(monkeypatch, changes)
        out = scorer._score_onchain()
        assert not any("partial" in s for s in out["signals"])
        assert any("tvl_fallback" in s for s in out["signals"])
