"""Tests for config loading."""

from app.config import settings


def test_settings_loads():
    assert settings.secret_key is not None
    assert settings.database_url is not None


def test_database_url_format():
    assert "postgresql" in settings.database_url


def test_celery_broker_url():
    assert settings.celery_broker_url is not None
    assert "redis" in settings.celery_broker_url
