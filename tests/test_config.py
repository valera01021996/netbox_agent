"""Tests for configuration module."""

import os
from datetime import timedelta

import pytest

from netbox_agent.config import Settings, parse_duration, reset_settings


class TestParseDuration:
    """Tests for duration parsing."""

    def test_seconds(self):
        assert parse_duration("30s") == timedelta(seconds=30)

    def test_minutes(self):
        assert parse_duration("15m") == timedelta(minutes=15)

    def test_hours(self):
        assert parse_duration("6h") == timedelta(hours=6)

    def test_days(self):
        assert parse_duration("2d") == timedelta(days=2)

    def test_invalid_format(self):
        with pytest.raises(ValueError):
            parse_duration("invalid")

    def test_missing_unit(self):
        with pytest.raises(ValueError):
            parse_duration("30")


class TestSettings:
    """Tests for Settings class."""

    def setup_method(self):
        """Reset settings before each test."""
        reset_settings()

    def test_required_fields(self, monkeypatch):
        """Test that required fields are enforced."""
        monkeypatch.setenv("NETBOX_URL", "https://netbox.example.com")
        monkeypatch.setenv("NETBOX_TOKEN", "test-token")
        monkeypatch.setenv("MM_WEBHOOK_URL", "https://mm.example.com/hooks/xxx")

        settings = Settings()
        assert settings.netbox_url == "https://netbox.example.com"
        assert settings.netbox_token == "test-token"

    def test_default_values(self, monkeypatch):
        """Test default values are set correctly."""
        monkeypatch.setenv("NETBOX_URL", "https://netbox.example.com")
        monkeypatch.setenv("NETBOX_TOKEN", "test-token")
        monkeypatch.setenv("MM_WEBHOOK_URL", "https://mm.example.com/hooks/xxx")

        settings = Settings()
        assert settings.poll_interval == 300
        assert settings.move_confirm_runs == 2
        assert settings.snmp_community == "public"

    def test_get_uplink_ports(self, monkeypatch):
        """Test uplink ports parsing."""
        monkeypatch.setenv("NETBOX_URL", "https://netbox.example.com")
        monkeypatch.setenv("NETBOX_TOKEN", "test-token")
        monkeypatch.setenv("MM_WEBHOOK_URL", "https://mm.example.com/hooks/xxx")
        monkeypatch.setenv("UPLINK_PORTS", "Eth49, Eth50, Eth51")

        settings = Settings()
        ports = settings.get_uplink_ports()
        assert ports == {"Eth49", "Eth50", "Eth51"}

    def test_get_mlag_groups(self, monkeypatch):
        """Test MLAG groups parsing."""
        monkeypatch.setenv("NETBOX_URL", "https://netbox.example.com")
        monkeypatch.setenv("NETBOX_TOKEN", "test-token")
        monkeypatch.setenv("MM_WEBHOOK_URL", "https://mm.example.com/hooks/xxx")
        monkeypatch.setenv("MLAG_GROUPS", '{"pair1": ["sw1", "sw2"]}')

        settings = Settings()
        groups = settings.get_mlag_groups()
        assert groups == {"pair1": ["sw1", "sw2"]}

    def test_parse_selector_role(self, monkeypatch):
        """Test role selector parsing."""
        monkeypatch.setenv("NETBOX_URL", "https://netbox.example.com")
        monkeypatch.setenv("NETBOX_TOKEN", "test-token")
        monkeypatch.setenv("MM_WEBHOOK_URL", "https://mm.example.com/hooks/xxx")

        settings = Settings()
        result = settings.parse_selector("role:server")
        assert result == {"role": "server"}

    def test_parse_selector_tag(self, monkeypatch):
        """Test tag selector parsing."""
        monkeypatch.setenv("NETBOX_URL", "https://netbox.example.com")
        monkeypatch.setenv("NETBOX_TOKEN", "test-token")
        monkeypatch.setenv("MM_WEBHOOK_URL", "https://mm.example.com/hooks/xxx")

        settings = Settings()
        result = settings.parse_selector("tag:monitored")
        assert result == {"tag": "monitored"}

    def test_parse_selector_invalid(self, monkeypatch):
        """Test invalid selector raises error."""
        monkeypatch.setenv("NETBOX_URL", "https://netbox.example.com")
        monkeypatch.setenv("NETBOX_TOKEN", "test-token")
        monkeypatch.setenv("MM_WEBHOOK_URL", "https://mm.example.com/hooks/xxx")

        settings = Settings()
        with pytest.raises(ValueError):
            settings.parse_selector("invalid")
