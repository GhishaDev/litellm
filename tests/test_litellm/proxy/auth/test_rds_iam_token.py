"""Tests for ``init_iam_db_url_from_env``.

The helper assembles ``DATABASE_URL`` (and optionally
``DATABASE_URL_READ_REPLICA``) from RDS IAM env vars before Prisma
initializes. The reader URL is opt-in via ``DATABASE_HOST_READ_REPLICA`` and
must not clobber any pre-existing ``DATABASE_URL_READ_REPLICA`` — operators
with a non-IAM read replica (or a precomputed URL) should keep what they set.
"""

from unittest.mock import patch

import pytest

from litellm.proxy.auth.rds_iam_token import init_iam_db_url_from_env


@pytest.fixture(autouse=True)
def _scrub_db_env(monkeypatch):
    """Remove every env var the helper reads so tests start from a clean slate."""
    for var in (
        "IAM_TOKEN_DB_AUTH",
        "DATABASE_URL",
        "DATABASE_URL_READ_REPLICA",
        "DATABASE_HOST",
        "DATABASE_PORT",
        "DATABASE_USER",
        "DATABASE_NAME",
        "DATABASE_SCHEMA",
        "DATABASE_HOST_READ_REPLICA",
        "DATABASE_PORT_READ_REPLICA",
        "DATABASE_USER_READ_REPLICA",
        "DATABASE_NAME_READ_REPLICA",
        "DATABASE_SCHEMA_READ_REPLICA",
    ):
        monkeypatch.delenv(var, raising=False)


def _stub_iam_token(token: str = "FAKE_TOKEN"):
    """Patch the AWS-touching token mint so tests don't need boto3 / network."""
    return patch(
        "litellm.proxy.auth.rds_iam_token.generate_iam_auth_token",
        return_value=token,
    )


def test_returns_false_when_iam_disabled(monkeypatch):
    """No env mutation, no error — just a False return."""
    assert init_iam_db_url_from_env() is False


def test_assembles_writer_url_when_iam_enabled(monkeypatch):
    monkeypatch.setenv("IAM_TOKEN_DB_AUTH", "true")
    monkeypatch.setenv("DATABASE_HOST", "writer.example.com")
    monkeypatch.setenv("DATABASE_USER", "litellm")
    monkeypatch.setenv("DATABASE_NAME", "litellm_db")

    with _stub_iam_token("WRITER_TOKEN"):
        assert init_iam_db_url_from_env() is True

    import os

    assert (
        os.environ["DATABASE_URL"]
        == "postgresql://litellm:WRITER_TOKEN@writer.example.com:5432/litellm_db"
    )
    # Reader was never configured, so it must not have been set.
    assert "DATABASE_URL_READ_REPLICA" not in os.environ


def test_missing_writer_envs_raises(monkeypatch):
    monkeypatch.setenv("IAM_TOKEN_DB_AUTH", "true")
    # DATABASE_HOST intentionally unset.
    monkeypatch.setenv("DATABASE_USER", "litellm")
    monkeypatch.setenv("DATABASE_NAME", "litellm_db")

    with pytest.raises(RuntimeError, match="DATABASE_HOST"):
        init_iam_db_url_from_env()


def test_reader_url_assembled_when_host_set_and_url_unset(monkeypatch):
    monkeypatch.setenv("IAM_TOKEN_DB_AUTH", "true")
    monkeypatch.setenv("DATABASE_HOST", "writer.example.com")
    monkeypatch.setenv("DATABASE_USER", "litellm")
    monkeypatch.setenv("DATABASE_NAME", "litellm_db")
    monkeypatch.setenv("DATABASE_HOST_READ_REPLICA", "reader.example.com")

    with _stub_iam_token("READER_TOKEN"):
        init_iam_db_url_from_env()

    import os

    assert (
        os.environ["DATABASE_URL_READ_REPLICA"]
        == "postgresql://litellm:READER_TOKEN@reader.example.com:5432/litellm_db"
    )


def test_reader_url_not_clobbered_when_already_set(monkeypatch):
    """If the operator pinned DATABASE_URL_READ_REPLICA (e.g. a non-IAM reader),
    the helper must leave it untouched even though DATABASE_HOST_READ_REPLICA
    is also set."""
    monkeypatch.setenv("IAM_TOKEN_DB_AUTH", "true")
    monkeypatch.setenv("DATABASE_HOST", "writer.example.com")
    monkeypatch.setenv("DATABASE_USER", "litellm")
    monkeypatch.setenv("DATABASE_NAME", "litellm_db")
    monkeypatch.setenv("DATABASE_HOST_READ_REPLICA", "reader.example.com")
    monkeypatch.setenv(
        "DATABASE_URL_READ_REPLICA",
        "postgresql://app:secret@reader.example.com:5432/litellm_db",
    )

    with _stub_iam_token("READER_TOKEN"):
        init_iam_db_url_from_env()

    import os

    assert (
        os.environ["DATABASE_URL_READ_REPLICA"]
        == "postgresql://app:secret@reader.example.com:5432/litellm_db"
    )


def test_reader_url_skipped_when_host_unset(monkeypatch):
    monkeypatch.setenv("IAM_TOKEN_DB_AUTH", "true")
    monkeypatch.setenv("DATABASE_HOST", "writer.example.com")
    monkeypatch.setenv("DATABASE_USER", "litellm")
    monkeypatch.setenv("DATABASE_NAME", "litellm_db")

    with _stub_iam_token("WRITER_TOKEN"):
        init_iam_db_url_from_env()

    import os

    assert "DATABASE_URL_READ_REPLICA" not in os.environ


def test_reader_field_fallbacks_default_to_writer_values(monkeypatch):
    """When *_READ_REPLICA fields are unset (other than host), they fall
    back to the writer's user / name / schema."""
    monkeypatch.setenv("IAM_TOKEN_DB_AUTH", "true")
    monkeypatch.setenv("DATABASE_HOST", "writer.example.com")
    monkeypatch.setenv("DATABASE_USER", "litellm")
    monkeypatch.setenv("DATABASE_NAME", "litellm_db")
    monkeypatch.setenv("DATABASE_SCHEMA", "public")
    monkeypatch.setenv("DATABASE_HOST_READ_REPLICA", "reader.example.com")

    with _stub_iam_token("READER_TOKEN"):
        init_iam_db_url_from_env()

    import os

    assert (
        os.environ["DATABASE_URL_READ_REPLICA"]
        == "postgresql://litellm:READER_TOKEN@reader.example.com:5432/litellm_db?schema=public"
    )
