"""
Unit tests for provider-side prompt cache token Prometheus metrics.

Run with:
    uv run pytest tests/test_litellm/integrations/test_prometheus_prompt_cache_token_metrics.py -v
"""

from typing import get_args
from unittest.mock import MagicMock

import pytest

from litellm.integrations.prometheus import PrometheusLogger
from litellm.types.integrations.prometheus import (
    DEFINED_PROMETHEUS_METRICS,
    PrometheusMetricLabels,
    UserAPIKeyLabelValues,
)

_PROMPT_CACHE_READ_LABELS = [
    "model",
    "api_provider",
    "hashed_api_key",
    "api_key_alias",
    "team",
    "team_alias",
    "end_user",
    "user",
    "model_id",
]

_PROMPT_CACHE_CREATION_LABELS = _PROMPT_CACHE_READ_LABELS + ["cache_ttl"]


@pytest.fixture
def sample_enum_values() -> UserAPIKeyLabelValues:
    return UserAPIKeyLabelValues(
        end_user="test-end-user",
        hashed_api_key="test-key-hash",
        api_key_alias="test-key-alias",
        team="test-team",
        team_alias="test-team-alias",
        user="test-user",
        model="claude-3-5-sonnet-20241022",
        api_provider="anthropic",
        model_id="model-id-123",
    )


def _build_mock_logger() -> MagicMock:
    mock_logger = MagicMock()
    mock_logger.litellm_prompt_cache_read_tokens_metric = MagicMock()
    mock_logger.litellm_prompt_cache_creation_tokens_metric = MagicMock()

    def _labels_for(metric_name: str):
        if metric_name == "litellm_prompt_cache_creation_tokens_metric":
            return _PROMPT_CACHE_CREATION_LABELS
        return _PROMPT_CACHE_READ_LABELS

    mock_logger.get_labels_for_metric = MagicMock(side_effect=_labels_for)
    return mock_logger


class TestPromptCacheTokenMetricsRegistration:
    """Schema-level checks: metrics + labels are wired correctly."""

    def test_metrics_defined_in_types(self):
        defined = get_args(DEFINED_PROMETHEUS_METRICS)
        assert "litellm_prompt_cache_read_tokens_metric" in defined
        assert "litellm_prompt_cache_creation_tokens_metric" in defined

    def test_read_metric_labels(self):
        labels = PrometheusMetricLabels.litellm_prompt_cache_read_tokens_metric
        for expected in ["model", "api_provider", "hashed_api_key", "team", "user"]:
            assert expected in labels
        # cache_ttl is creation-only
        assert "cache_ttl" not in labels

    def test_creation_metric_labels_include_cache_ttl(self):
        labels = PrometheusMetricLabels.litellm_prompt_cache_creation_tokens_metric
        assert "cache_ttl" in labels
        assert "api_provider" in labels


class TestPromptCacheTokenMetricsIncrement:
    """Behavior of _increment_prompt_cache_token_metrics across providers."""

    def test_anthropic_with_ttl_split(self, sample_enum_values):
        """Anthropic response with both 5m and 1h cache_creation buckets -> 2 series + read."""
        mock_logger = _build_mock_logger()
        payload = {
            "metadata": {
                "usage_object": {
                    "prompt_tokens_details": {
                        "cached_tokens": 4096,
                        "cache_creation_tokens": 5120,
                        "cache_creation_token_details": {
                            "ephemeral_5m_input_tokens": 3120,
                            "ephemeral_1h_input_tokens": 2000,
                        },
                    }
                }
            }
        }

        PrometheusLogger._increment_prompt_cache_token_metrics(
            mock_logger,
            standard_logging_payload=payload,
            enum_values=sample_enum_values,
        )

        # cache read: single inc with 4096
        mock_logger.litellm_prompt_cache_read_tokens_metric.labels().inc.assert_called_once_with(
            4096.0
        )
        # cache creation: 2 calls (5m + 1h)
        creation = mock_logger.litellm_prompt_cache_creation_tokens_metric
        assert creation.labels.call_count >= 2
        inc_calls = creation.labels().inc.call_args_list
        amounts = sorted(call.args[0] for call in inc_calls)
        assert amounts == [2000.0, 3120.0]

    def test_anthropic_without_ttl_split_falls_back_to_unknown(
        self, sample_enum_values
    ):
        """Older Anthropic API: cache_creation_tokens set but no cache_creation_token_details."""
        mock_logger = _build_mock_logger()
        payload = {
            "metadata": {
                "usage_object": {
                    "prompt_tokens_details": {
                        "cached_tokens": 0,
                        "cache_creation_tokens": 1500,
                    }
                }
            }
        }

        PrometheusLogger._increment_prompt_cache_token_metrics(
            mock_logger,
            standard_logging_payload=payload,
            enum_values=sample_enum_values,
        )

        # No read (cached_tokens=0)
        mock_logger.litellm_prompt_cache_read_tokens_metric.labels.assert_not_called()
        # Capture the labels call from production code BEFORE any assert probes
        # that themselves record empty ``labels()`` calls on the mock.
        creation = mock_logger.litellm_prompt_cache_creation_tokens_metric
        production_calls = list(creation.labels.call_args_list)
        assert len(production_calls) == 1
        assert production_calls[0].kwargs.get("cache_ttl") == "unknown"
        # And the inc was called with the full amount.
        creation.labels().inc.assert_called_once_with(1500.0)

    def test_openai_cached_tokens_only(self, sample_enum_values):
        """OpenAI: only cached_tokens; creation metric must NOT fire."""
        mock_logger = _build_mock_logger()
        payload = {
            "metadata": {
                "usage_object": {
                    "prompt_tokens_details": {
                        "cached_tokens": 2048,
                    }
                }
            }
        }

        PrometheusLogger._increment_prompt_cache_token_metrics(
            mock_logger,
            standard_logging_payload=payload,
            enum_values=sample_enum_values,
        )

        mock_logger.litellm_prompt_cache_read_tokens_metric.labels().inc.assert_called_once_with(
            2048.0
        )
        mock_logger.litellm_prompt_cache_creation_tokens_metric.labels.assert_not_called()

    def test_no_cache_fields(self, sample_enum_values):
        """Request with no prompt caching activity: no series produced."""
        mock_logger = _build_mock_logger()
        payload = {
            "metadata": {
                "usage_object": {
                    "prompt_tokens_details": {"cached_tokens": 0},
                }
            }
        }

        PrometheusLogger._increment_prompt_cache_token_metrics(
            mock_logger,
            standard_logging_payload=payload,
            enum_values=sample_enum_values,
        )

        mock_logger.litellm_prompt_cache_read_tokens_metric.labels.assert_not_called()
        mock_logger.litellm_prompt_cache_creation_tokens_metric.labels.assert_not_called()

    def test_missing_prompt_tokens_details_is_safe(self, sample_enum_values):
        """Payload without usage_object / prompt_tokens_details must no-op."""
        mock_logger = _build_mock_logger()

        for payload in (
            {},
            {"metadata": {}},
            {"metadata": {"usage_object": {}}},
            {"metadata": {"usage_object": {"prompt_tokens_details": None}}},
            {"metadata": {"usage_object": {"prompt_tokens_details": "bad"}}},
        ):
            PrometheusLogger._increment_prompt_cache_token_metrics(
                mock_logger,
                standard_logging_payload=payload,
                enum_values=sample_enum_values,
            )

        mock_logger.litellm_prompt_cache_read_tokens_metric.labels.assert_not_called()
        mock_logger.litellm_prompt_cache_creation_tokens_metric.labels.assert_not_called()

    def test_zero_ttl_buckets_skipped(self, sample_enum_values):
        """If 5m=0 and 1h=N, only 1h series fires."""
        mock_logger = _build_mock_logger()
        payload = {
            "metadata": {
                "usage_object": {
                    "prompt_tokens_details": {
                        "cached_tokens": 0,
                        "cache_creation_tokens": 800,
                        "cache_creation_token_details": {
                            "ephemeral_5m_input_tokens": 0,
                            "ephemeral_1h_input_tokens": 800,
                        },
                    }
                }
            }
        }

        PrometheusLogger._increment_prompt_cache_token_metrics(
            mock_logger,
            standard_logging_payload=payload,
            enum_values=sample_enum_values,
        )

        creation = mock_logger.litellm_prompt_cache_creation_tokens_metric
        production_calls = list(creation.labels.call_args_list)
        assert len(production_calls) == 1
        assert production_calls[0].kwargs.get("cache_ttl") == "1h"
        creation.labels().inc.assert_called_once_with(800.0)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
