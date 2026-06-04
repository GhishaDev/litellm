"""
Unit tests for the cache_ttl-aware emission of
`litellm_input_cache_creation_tokens_metric`.

Per the v1.87.0 migration, the read side
(`litellm_input_cached_tokens_metric`) is emitted by the upstream
`_increment_detail_token_metrics` generic loop and has no TTL concept.
This file only covers the cache_creation path that
`_increment_prompt_cache_token_metrics` owns.

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
    mock_logger.litellm_input_cache_creation_tokens_metric = MagicMock()
    # Mirror the production label list so prometheus_label_factory passes
    # cache_ttl through into the .labels(**kwargs) call we assert against.
    mock_logger.get_labels_for_metric = MagicMock(
        return_value=PrometheusMetricLabels.litellm_input_cache_creation_tokens_metric,
    )
    return mock_logger


class TestCreationMetricLabelSchema:
    """The migrated metric carries the `cache_ttl` label."""

    def test_metric_defined_in_types(self):
        defined = get_args(DEFINED_PROMETHEUS_METRICS)
        assert "litellm_input_cache_creation_tokens_metric" in defined

    def test_creation_metric_labels_include_cache_ttl(self):
        labels = PrometheusMetricLabels.litellm_input_cache_creation_tokens_metric
        assert "cache_ttl" in labels


class TestCreationMetricEmission:
    """Behavior of _increment_prompt_cache_token_metrics across response shapes."""

    def test_anthropic_with_ttl_split(self, sample_enum_values):
        """5m and 1h buckets → 2 series, one per non-zero TTL."""
        mock_logger = _build_mock_logger()
        payload = {
            "metadata": {
                "usage_object": {
                    "prompt_tokens_details": {
                        "cache_creation_tokens": 800,
                        "cache_creation_token_details": {
                            "ephemeral_5m_input_tokens": 500,
                            "ephemeral_1h_input_tokens": 300,
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
        creation = mock_logger.litellm_input_cache_creation_tokens_metric
        # Two emissions, one per TTL bucket
        assert creation.labels.call_count == 2
        ttl_labels_emitted = {
            call.kwargs.get("cache_ttl") for call in creation.labels.call_args_list
        }
        assert ttl_labels_emitted == {"5m", "1h"}

    def test_anthropic_5m_only_skips_zero_1h_bucket(self, sample_enum_values):
        mock_logger = _build_mock_logger()
        payload = {
            "metadata": {
                "usage_object": {
                    "prompt_tokens_details": {
                        "cache_creation_tokens": 500,
                        "cache_creation_token_details": {
                            "ephemeral_5m_input_tokens": 500,
                            "ephemeral_1h_input_tokens": 0,
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
        creation = mock_logger.litellm_input_cache_creation_tokens_metric
        assert creation.labels.call_count == 1
        assert creation.labels.call_args.kwargs.get("cache_ttl") == "5m"

    def test_no_ttl_breakdown_falls_back_to_unknown(self, sample_enum_values):
        """Older Anthropic API / non-Anthropic — emit one series with cache_ttl='unknown'."""
        mock_logger = _build_mock_logger()
        payload = {
            "metadata": {
                "usage_object": {
                    "prompt_tokens_details": {
                        "cache_creation_tokens": 1200,
                    }
                }
            }
        }
        PrometheusLogger._increment_prompt_cache_token_metrics(
            mock_logger,
            standard_logging_payload=payload,
            enum_values=sample_enum_values,
        )
        creation = mock_logger.litellm_input_cache_creation_tokens_metric
        assert creation.labels.call_count == 1
        assert creation.labels.call_args.kwargs.get("cache_ttl") == "unknown"

    def test_zero_creation_skips_emit(self, sample_enum_values):
        mock_logger = _build_mock_logger()
        payload = {
            "metadata": {
                "usage_object": {
                    "prompt_tokens_details": {
                        "cache_creation_tokens": 0,
                    }
                }
            }
        }
        PrometheusLogger._increment_prompt_cache_token_metrics(
            mock_logger,
            standard_logging_payload=payload,
            enum_values=sample_enum_values,
        )
        mock_logger.litellm_input_cache_creation_tokens_metric.labels.assert_not_called()

    def test_missing_prompt_tokens_details_skips_emit(self, sample_enum_values):
        mock_logger = _build_mock_logger()
        payload = {"metadata": {"usage_object": {}}}
        PrometheusLogger._increment_prompt_cache_token_metrics(
            mock_logger,
            standard_logging_payload=payload,
            enum_values=sample_enum_values,
        )
        mock_logger.litellm_input_cache_creation_tokens_metric.labels.assert_not_called()

    def test_missing_usage_object_skips_emit(self, sample_enum_values):
        mock_logger = _build_mock_logger()
        payload = {"metadata": {}}
        PrometheusLogger._increment_prompt_cache_token_metrics(
            mock_logger,
            standard_logging_payload=payload,
            enum_values=sample_enum_values,
        )
        mock_logger.litellm_input_cache_creation_tokens_metric.labels.assert_not_called()
