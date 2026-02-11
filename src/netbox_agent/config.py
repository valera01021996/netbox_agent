"""Configuration management using Pydantic Settings."""

import json
import re
from datetime import timedelta
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_duration(value: str) -> timedelta:
    """Parse duration string like '6h', '30m', '1d' to timedelta."""
    match = re.match(r"^(\d+)([smhd])$", value.strip().lower())
    if not match:
        raise ValueError(f"Invalid duration format: {value}. Use format like '6h', '30m', '1d'")

    amount = int(match.group(1))
    unit = match.group(2)

    units = {
        "s": timedelta(seconds=amount),
        "m": timedelta(minutes=amount),
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
    }
    return units[unit]


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # NetBox configuration
    netbox_url: str = Field(..., description="NetBox API URL")
    netbox_token: str = Field(..., description="NetBox API token")
    netbox_verify_ssl: bool = Field(True, description="Verify SSL certificates")

    # Switch selector (servers are auto-detected by OOB IP presence)
    switches_selector: str = Field(
        "role:switch", description="Selector for switches (role:X or tag:Y)"
    )

    # Polling configuration
    poll_interval: int = Field(300, description="Polling interval in seconds", ge=60)
    move_confirm_runs: int = Field(
        2, description="Number of consecutive runs to confirm a move", ge=1
    )

    # SNMP configuration
    snmp_community: str = Field("public", description="SNMP community string")
    snmp_version: str = Field("2c", description="SNMP version (2c or 3)")
    snmp_timeout: int = Field(5, description="SNMP timeout in seconds", ge=1)
    snmp_retries: int = Field(2, description="SNMP retry count", ge=0)

    # Uplink detection
    uplink_ports: str = Field("", description="Comma-separated list of uplink port names")
    uplink_patterns: str = Field(
        "uplink,trunk,lag,po", description="Comma-separated patterns for uplink detection"
    )

    # MLAG groups (JSON string)
    mlag_groups: str = Field("{}", description="MLAG groups as JSON")

    # State database
    state_db_path: str = Field("./state.db", description="Path to SQLite database")
    db_dsn: str | None = Field(None, description="PostgreSQL connection string")

    # Alert deduplication
    remind_after: str = Field("6h", description="Time before re-sending reminder")

    # Tag-based alerting for NetBox Webhooks
    move_tag_name: str = Field(
        "ipmi-moved", description="Tag name to add on device when IPMI move is detected"
    )

    # Logging
    log_level: str = Field("INFO", description="Log level")
    log_format: str = Field("json", description="Log format (json or text)")

    @field_validator("mlag_groups")
    @classmethod
    def validate_mlag_groups(cls, v: str) -> str:
        """Validate MLAG groups is valid JSON."""
        try:
            json.loads(v)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON for MLAG_GROUPS: {e}") from e
        return v

    @field_validator("remind_after")
    @classmethod
    def validate_remind_after(cls, v: str) -> str:
        """Validate remind_after duration format."""
        parse_duration(v)  # Will raise if invalid
        return v

    def get_uplink_ports(self) -> set[str]:
        """Get set of uplink port names."""
        if not self.uplink_ports:
            return set()
        return {p.strip() for p in self.uplink_ports.split(",") if p.strip()}

    def get_uplink_patterns(self) -> list[re.Pattern[str]]:
        """Get compiled regex patterns for uplink detection."""
        if not self.uplink_patterns:
            return []
        patterns = []
        for p in self.uplink_patterns.split(","):
            p = p.strip()
            if p:
                patterns.append(re.compile(p, re.IGNORECASE))
        return patterns

    def get_mlag_groups(self) -> dict[str, list[str]]:
        """Get MLAG groups as dict."""
        return json.loads(self.mlag_groups)

    def get_remind_after_timedelta(self) -> timedelta:
        """Get remind_after as timedelta."""
        return parse_duration(self.remind_after)

    def parse_selector(self, selector: str) -> dict[str, Any]:
        """
        Parse selector string like 'role:server' or 'tag:ipmi-monitored'.

        Returns dict with filter parameters for NetBox API.
        """
        if ":" not in selector:
            raise ValueError(f"Invalid selector format: {selector}. Use 'role:X' or 'tag:Y'")

        key, value = selector.split(":", 1)
        key = key.strip().lower()
        value = value.strip()

        if key == "role":
            return {"role": value}
        elif key == "tag":
            return {"tag": value}
        elif key == "site":
            return {"site": value}
        else:
            raise ValueError(f"Unknown selector type: {key}. Use 'role', 'tag', or 'site'")


# Global settings instance (lazy loaded)
_settings: Settings | None = None


def get_settings() -> Settings:
    """Get or create settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings


def reset_settings() -> None:
    """Reset settings (for testing)."""
    global _settings
    _settings = None
