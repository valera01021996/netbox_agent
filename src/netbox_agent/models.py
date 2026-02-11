"""Data models for NetBox IPMI Agent."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class MoveStatus(str, Enum):
    """Status of a MAC move detection."""

    OK = "ok"  # MAC is on expected port
    OK_MLAG_PEER = "ok_mlag_peer"  # MAC is on MLAG peer (acceptable)
    SUSPECT_UPLINK = "suspect_uplink"  # MAC found on uplink (noise)
    MOVE_DETECTED = "move_detected"  # MAC is on unexpected port (unconfirmed)
    MOVE_CONFIRMED = "move_confirmed"  # MAC move confirmed (N cycles)
    NOT_FOUND = "not_found"  # MAC not found in FDB


@dataclass
class IpmiInterface:
    """IPMI interface information from NetBox."""

    device_id: int
    device_name: str
    interface_id: int
    interface_name: str
    mac_address: str
    ip_address: str | None = None
    netbox_url: str | None = None  # Link to device in NetBox


@dataclass
class ExpectedEndpoint:
    """Expected cable endpoint from NetBox."""

    switch_id: int
    switch_name: str
    port_id: int
    port_name: str
    cable_id: int | None = None
    netbox_url: str | None = None  # Link to switch in NetBox


@dataclass
class ServerIpmi:
    """Server with IPMI interface and expected endpoint."""

    interface: IpmiInterface
    expected_endpoint: ExpectedEndpoint | None = None

    @property
    def mac(self) -> str:
        return self.interface.mac_address

    @property
    def server_name(self) -> str:
        return self.interface.device_name


@dataclass
class ObservedEndpoint:
    """Observed MAC location from FDB."""

    switch_name: str
    port_name: str
    vlan: int | None = None
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def matches(self, expected: ExpectedEndpoint | None) -> bool:
        """Check if this observed endpoint matches expected."""
        if expected is None:
            return False
        return (
            self.switch_name.lower() == expected.switch_name.lower()
            and self.port_name.lower() == expected.port_name.lower()
        )


@dataclass
class FdbEntry:
    """Single FDB (MAC address table) entry."""

    mac_address: str
    port_name: str
    vlan: int | None = None


@dataclass
class SwitchFdb:
    """FDB entries from a switch."""

    switch_name: str
    entries: list[FdbEntry] = field(default_factory=list)
    collected_at: datetime = field(default_factory=datetime.utcnow)
    error: str | None = None  # If collection failed


@dataclass
class MoveEvent:
    """A detected IPMI move event."""

    server: ServerIpmi
    expected: ExpectedEndpoint | None
    observed: ObservedEndpoint | None
    status: MoveStatus
    consecutive_count: int = 1
    first_seen: datetime = field(default_factory=datetime.utcnow)
    last_seen: datetime = field(default_factory=datetime.utcnow)


@dataclass
class AlertInfo:
    """Information for an alert to be sent."""

    server_name: str
    server_url: str | None
    mac_address: str
    ip_address: str | None
    expected_switch: str
    expected_port: str
    expected_url: str | None
    observed_switch: str
    observed_port: str
    observed_vlan: int | None
    consecutive_count: int
    first_detected: datetime
    is_reminder: bool = False
