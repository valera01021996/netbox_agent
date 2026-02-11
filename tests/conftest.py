"""Pytest configuration and fixtures."""

import pytest

from netbox_agent.config import reset_settings


@pytest.fixture(autouse=True)
def reset_settings_fixture():
    """Reset settings before each test."""
    reset_settings()
    yield
    reset_settings()
