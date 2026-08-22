"""
Centralized LLM Client with automatic fallback.

Primary: DeepSeek (deepseek-v4-pro)
Fallback: Disabled (single provider)
Second Opinion: Disabled

On timeout, rate-limit (429), 5xx, or connection errors, automatically
tries the fallback provider.

Usage:
    from src.llm_client import LLMClient

    client = LLMClient()
    response = client.chat(
        messages=[{"role": "user", "content": "Hello"}],
        model="deepseek-v4-pro",
    )
    # Returns: {"content": "...", "provider": "deepseek"} or None on total failure
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration loader
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"
_config_cache: Optional[Dict] = None


def _load_config() -> Dict:
    """Load LLM config from config/config.yaml with env-var overrides."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    cfg: Dict[str, Any] = {
        "llm": {
            "primary": {
                "provider": "deepseek",
                "model": "deepseek-v4-pro",
                "base_url": "https://api.deepseek.com/v1",
                "timeout": 30,
            },
            "fallback": {
                "provider": "deepseek",
                "model": "deepseek-v4-pro",
                "base_url": "https://api.deepseek.com/v1",
                "timeout": 30,
                "enabled": False,
            },
            "retry": {"max_retries": 1, "retry_delay": 1.0},
            "fallback_triggers": {
                "timeout": True,
                "rate_limit_429": True,
                "server_error_5xx": True,
                "connection_error": True,
            },
        }
    }

    try:
        if _CONFIG_PATH.exists():
            import yaml

            with open(_CONFIG_PATH, "r") as f:
                file_cfg = yaml.safe_load(f) or {}
            if "llm" in file_cfg:
                # Deep-merge LLM section
                for key in file_cfg["llm"]:
                    cfg["llm"][key] = file_cfg["llm"][key]
    except Exception as e:
        logger.warning("Failed to load LLM config from %s: %s", _CONFIG_PATH, e)

    # Env-var overrides
    if os.environ.get("DEEPSEEK_API_KEY"):
        cfg["llm"]["primary"]["base_url"] = os.environ.get(
            "DEEPSEEK_BASE_URL", cfg["llm"]["primary"]["base_url"]
        )
    if os.environ.get("DEEPSEEK_API_KEY"):
        cfg["llm"]["fallback"]["enabled"] = False  # single provider

    _config_cache = cfg
    return cfg


# ---------------------------------------------------------------------------
# Provider definitions
# ---------------------------------------------------------------------------


def _get_provider_config(provider_name: str) -> Tuple[Dict, str]:
    """Return (provider_config_dict, api_key) for a named provider."""
    cfg = _load_config()
    pcfg = cfg["llm"].get(provider_name, {})
    provider = pcfg.get("provider", provider_name)

    env_key_map = {
        "deepseek": (
            "DEEPSEEK_API_KEY",
            "DEEPSEEK_BASE_URL",
            "https://api.deepseek.com/v1",
        ),
        "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "kimi": ("KIMI_API_KEY", "KIMI_BASE_URL", "https://api.moonshot.cn/v1"),
        "zhipu": ("GLM_API_KEY", "GLM_BASE_URL", "https://api.z.ai/api/coding/paas/v4"),
    }

    if provider in env_key_map:
        api_key_env, base_url_env, default_url = env_key_map[provider]
        api_key = os.environ.get(api_key_env, "")
        base_url = os.environ.get(base_url_env, pcfg.get("base_url", default_url))
    else:
        api_key = os.environ.get(f"{provider.upper()}_API_KEY", "")
        base_url = pcfg.get("base_url", "")

    return {
        "provider": provider,
        "model": pcfg.get("model", "deepseek-v4-pro"),
        "base_url": base_url,
        "timeout": pcfg.get("timeout", 30),
    }, api_key


# ---------------------------------------------------------------------------
# Core client
# ---------------------------------------------------------------------------


class LLMClient:
    """Centralized LLM client with automatic provider fallback.

    Try primary provider first; on failure, fall back to the configured
    fallback provider.  Triggers: timeout, 429, 5xx, connection error.
    """

    def __init__(self, provider_cfg_name: str = "primary"):
        """Initialize the client.

        Args:
            provider_cfg_name: "primary" (DeepSeek) or "fallback" (OpenAI).
                               The client will start with primary and auto-fallback.
        """
        cfg = _load_config()["llm"]
        self._retry_cfg = cfg.get("retry", {"max_retries": 1, "retry_delay": 1.0})
        self._fallback_triggers = cfg.get("fallback_triggers", {})

        # Load both providers
        self._primary_cfg, self._primary_key = _get_provider_config("primary")
        self._fallback_cfg, self._fallback_key = _get_provider_config("fallback")
        self._fallback_enabled = cfg.get("fallback", {}).get("enabled", True)

        # Stats for observability
        self.stats = {"primary_calls": 0, "fallback_calls": 0, "total_failures": 0}

    # ----- public API -----

    def chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 512,
        system_prompt: Optional[str] = None,
        response_format_json: bool = False,
        reasoning_effort: Optional[str] = "none",
    ) -> Optional[Dict[str, Any]]:
        """Send a chat completion request with automatic fallback.

        Args:
            messages: List of {"role": ..., "content": ...} dicts.
            model: Override model name (optional).
            temperature: Sampling temperature.
            max_tokens: Max response tokens.
            system_prompt: If provided, prepended as system message.
            response_format_json: If True, request JSON output format.
            reasoning_effort: "none" (default) disables chain-of-thought for
                DeepSeek v4 reasoning models, so small max_tokens budgets are
                not consumed by thinking. Set None to leave model default.

        Returns:
            {"content": str, "provider": str, "model": str} or None on total failure.
        """
        # Build messages with optional system prompt
        full_messages = list(messages)
        if system_prompt:
            full_messages.insert(0, {"role": "system", "content": system_prompt})

        # --- Try primary ---
        result = self._try_provider(
            self._primary_cfg,
            self._primary_key,
            full_messages,
            model=model or self._primary_cfg["model"],
            temperature=temperature,
            max_tokens=max_tokens,
            response_format_json=response_format_json,
            reasoning_effort=reasoning_effort,
        )
        if result is not None:
            self.stats["primary_calls"] += 1
            return result

        # --- Fallback ---
        if not self._fallback_enabled or not self._fallback_key:
            logger.warning("LLMClient: primary failed, fallback disabled or no API key")
            self.stats["total_failures"] += 1
            return None

        logger.warning(
            "LLMClient: primary (%s) failed — falling back to %s",
            self._primary_cfg["provider"],
            self._fallback_cfg["provider"],
        )
        result = self._try_provider(
            self._fallback_cfg,
            self._fallback_key,
            full_messages,
            model=model or self._fallback_cfg["model"],
            temperature=temperature,
            max_tokens=max_tokens,
            response_format_json=response_format_json,
            reasoning_effort=reasoning_effort,
        )
        if result is not None:
            self.stats["fallback_calls"] += 1
            return result

        self.stats["total_failures"] += 1
        return None

    # ----- internal -----

    def _try_provider(
        self,
        provider_cfg: Dict,
        api_key: str,
        messages: List[Dict],
        model: str,
        temperature: float,
        max_tokens: int,
        response_format_json: bool,
        reasoning_effort: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Attempt a single provider with retries."""
        import time as _time

        if not api_key:
            return None

        provider_name = provider_cfg.get("provider", "unknown")
        base_url = provider_cfg["base_url"].rstrip("/")
        timeout = provider_cfg.get("timeout", 30)
        max_retries = self._retry_cfg.get("max_retries", 1)
        retry_delay = self._retry_cfg.get("retry_delay", 1.0)

        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format_json:
            payload["response_format"] = {"type": "json_object"}
        # DeepSeek v4 reasoning models: without this, thinking consumes the
        # entire max_tokens budget and content comes back empty (bug#20).
        # Other providers may reject the param, so only send it to deepseek.
        if reasoning_effort and provider_name == "deepseek":
            payload["reasoning_effort"] = reasoning_effort

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        last_error: Optional[Exception] = None
        for attempt in range(1 + max_retries):
            if attempt > 0:
                _time.sleep(retry_delay * attempt)

            try:
                resp = requests.post(
                    f"{base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                )

                # Check for trigger conditions
                if resp.status_code == 429 and self._fallback_triggers.get(
                    "rate_limit_429"
                ):
                    logger.warning(
                        "LLMClient [%s]: rate limited (429), attempt %d/%d",
                        provider_name,
                        attempt + 1,
                        1 + max_retries,
                    )
                    last_error = Exception(f"Rate limited (429) by {provider_name}")
                    continue  # retry, then fallback

                if resp.status_code >= 500 and self._fallback_triggers.get(
                    "server_error_5xx"
                ):
                    logger.warning(
                        "LLMClient [%s]: server error %d, attempt %d/%d",
                        provider_name,
                        resp.status_code,
                        attempt + 1,
                        1 + max_retries,
                    )
                    last_error = Exception(
                        f"Server error {resp.status_code} from {provider_name}"
                    )
                    continue

                if resp.status_code == 200:
                    return self._parse_response(resp.json(), provider_name)

                # Other errors (400, 401, etc.) — don't retry or fallback
                logger.warning(
                    "LLMClient [%s]: HTTP %d — not retryable",
                    provider_name,
                    resp.status_code,
                )
                return None

            except requests.exceptions.Timeout:
                if self._fallback_triggers.get("timeout"):
                    logger.warning(
                        "LLMClient [%s]: timeout, attempt %d/%d",
                        provider_name,
                        attempt + 1,
                        1 + max_retries,
                    )
                    last_error = Exception(f"Timeout from {provider_name}")
                    continue
                return None

            except requests.exceptions.ConnectionError as e:
                if self._fallback_triggers.get("connection_error"):
                    logger.warning(
                        "LLMClient [%s]: connection error, attempt %d/%d: %s",
                        provider_name,
                        attempt + 1,
                        1 + max_retries,
                        e,
                    )
                    last_error = e
                    continue
                return None

            except Exception as e:
                logger.warning("LLMClient [%s]: unexpected error: %s", provider_name, e)
                last_error = e
                return None

        # All retries exhausted
        if last_error:
            logger.warning(
                "LLMClient [%s]: all retries exhausted: %s", provider_name, last_error
            )
        return None

    @staticmethod
    def _parse_response(data: Dict, provider: str) -> Optional[Dict[str, Any]]:
        """Extract content from a chat completion response."""
        try:
            choice = data["choices"][0]["message"]
            content = choice.get("content", "") or ""
            if not content.strip():
                # Reasoning-only response (budget exhausted by thinking, or
                # model misbehaving). reasoning_content is a draft, never an
                # answer — treat as failure so caller/fallback can handle it.
                logger.warning(
                    "LLMClient: empty content from %s (finish=%s) — rejecting "
                    "reasoning-only response",
                    provider,
                    data["choices"][0].get("finish_reason"),
                )
                return None
            model_used = data.get("model", "unknown")
            return {
                "content": content.strip(),
                "provider": provider,
                "model": model_used,
            }
        except (KeyError, IndexError, TypeError) as e:
            logger.warning(
                "LLMClient: failed to parse response from %s: %s", provider, e
            )
            return None


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

_default_client: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    """Get or create the module-level default LLM client."""
    global _default_client
    if _default_client is None:
        _default_client = LLMClient()
    return _default_client


# ---------------------------------------------------------------------------
# Second opinion client — disabled (single DeepSeek provider)
# ---------------------------------------------------------------------------

_second_client = None


def get_second_opinion_client():
    """Second opinion disabled — single DeepSeek provider mode.

    Returns None; market_researcher falls back to single-model sentiment.
    """
    return None
