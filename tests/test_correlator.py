"""Tests for correlator module."""

from datetime import datetime

import pytest

from netbox_agent.correlator import Correlator
from netbox_agent.models import (
    ExpectedEndpoint,
    FdbEntry,
    IpmiInterface,
    MoveStatus,
    ServerIpmi,
    SwitchFdb,
)


@pytest.fixture
def mock_settings(monkeypatch):
    """Create mock settings."""
    monkeypatch.setenv("NETBOX_URL", "https://netbox.example.com")
    monkeypatch.setenv("NETBOX_TOKEN", "test-token")
    monkeypatch.setenv("MM_WEBHOOK_URL", "https://mm.example.com/hooks/xxx")
    monkeypatch.setenv("UPLINK_PORTS", "Ethernet49,Ethernet50")
    monkeypatch.setenv("UPLINK_PATTERNS", "uplink,trunk")
    monkeypatch.setenv("MLAG_GROUPS", '{"pair1": ["switch1", "switch2"]}')


def make_server(
    name: str,
    mac: str,
    expected_switch: str,
    expected_port: str,
) -> ServerIpmi:
    """Create a test server."""
    iface = IpmiInterface(
        device_id=1,
        device_name=name,
        interface_id=10,
        interface_name="IPMI",
        mac_address=mac,
    )
    expected = ExpectedEndpoint(
        switch_id=1,
        switch_name=expected_switch,
        port_id=10,
        port_name=expected_port,
    )
    return ServerIpmi(interface=iface, expected_endpoint=expected)


def make_fdb(
    switch_name: str,
    entries: list[tuple[str, str, int | None]],
) -> SwitchFdb:
    """Create FDB data for a switch."""
    return SwitchFdb(
        switch_name=switch_name,
        entries=[
            FdbEntry(mac_address=mac, port_name=port, vlan=vlan)
            for mac, port, vlan in entries
        ],
    )


class TestCorrelator:
    """Tests for Correlator class."""

    def test_mac_found_on_expected_port(self, mock_settings):
        """Test MAC on expected port returns OK."""
        correlator = Correlator()

        servers = [make_server("srv1", "aa:bb:cc:dd:ee:ff", "switch1", "Ethernet1")]
        fdb = {
            "switch1": make_fdb("switch1", [("aa:bb:cc:dd:ee:ff", "Ethernet1", 100)]),
        }

        events = correlator.correlate(servers, fdb)

        assert len(events) == 1
        assert events[0].status == MoveStatus.OK

    def test_mac_not_found(self, mock_settings):
        """Test MAC not found returns NOT_FOUND."""
        correlator = Correlator()

        servers = [make_server("srv1", "aa:bb:cc:dd:ee:ff", "switch1", "Ethernet1")]
        fdb = {
            "switch1": make_fdb("switch1", [("11:22:33:44:55:66", "Ethernet1", 100)]),
        }

        events = correlator.correlate(servers, fdb)

        assert len(events) == 1
        assert events[0].status == MoveStatus.NOT_FOUND

    def test_mac_on_different_port(self, mock_settings):
        """Test MAC on different port returns MOVE_DETECTED."""
        correlator = Correlator()

        servers = [make_server("srv1", "aa:bb:cc:dd:ee:ff", "switch1", "Ethernet1")]
        fdb = {
            "switch1": make_fdb("switch1", [("aa:bb:cc:dd:ee:ff", "Ethernet5", 100)]),
        }

        events = correlator.correlate(servers, fdb)

        assert len(events) == 1
        assert events[0].status == MoveStatus.MOVE_DETECTED
        assert events[0].observed.port_name == "Ethernet5"

    def test_mac_on_different_switch(self, mock_settings):
        """Test MAC on different switch returns MOVE_DETECTED."""
        correlator = Correlator()

        servers = [make_server("srv1", "aa:bb:cc:dd:ee:ff", "switch1", "Ethernet1")]
        fdb = {
            "switch1": make_fdb("switch1", []),
            "switch3": make_fdb("switch3", [("aa:bb:cc:dd:ee:ff", "Ethernet1", 100)]),
        }

        events = correlator.correlate(servers, fdb)

        assert len(events) == 1
        assert events[0].status == MoveStatus.MOVE_DETECTED
        assert events[0].observed.switch_name == "switch3"

    def test_mac_on_uplink_port(self, mock_settings):
        """Test MAC on uplink port returns SUSPECT_UPLINK."""
        correlator = Correlator()

        servers = [make_server("srv1", "aa:bb:cc:dd:ee:ff", "switch1", "Ethernet1")]
        fdb = {
            "switch1": make_fdb("switch1", [("aa:bb:cc:dd:ee:ff", "Ethernet49", 100)]),
        }

        events = correlator.correlate(servers, fdb)

        assert len(events) == 1
        assert events[0].status == MoveStatus.SUSPECT_UPLINK

    def test_mac_on_mlag_peer_same_port(self, mock_settings):
        """Test MAC on MLAG peer same port returns OK_MLAG_PEER."""
        correlator = Correlator()

        # Expected on switch1:Ethernet1, found on switch2:Ethernet1 (MLAG peers)
        servers = [make_server("srv1", "aa:bb:cc:dd:ee:ff", "switch1", "Ethernet1")]
        fdb = {
            "switch1": make_fdb("switch1", []),
            "switch2": make_fdb("switch2", [("aa:bb:cc:dd:ee:ff", "Ethernet1", 100)]),
        }

        events = correlator.correlate(servers, fdb)

        assert len(events) == 1
        assert events[0].status == MoveStatus.OK_MLAG_PEER

    def test_prefers_expected_switch(self, mock_settings):
        """Test that MAC on expected switch is preferred over other observations."""
        correlator = Correlator()

        servers = [make_server("srv1", "aa:bb:cc:dd:ee:ff", "switch1", "Ethernet1")]
        # MAC seen on both switches, but correct on switch1
        fdb = {
            "switch1": make_fdb("switch1", [("aa:bb:cc:dd:ee:ff", "Ethernet1", 100)]),
            "switch3": make_fdb("switch3", [("aa:bb:cc:dd:ee:ff", "Ethernet5", 200)]),
        }

        events = correlator.correlate(servers, fdb)

        assert len(events) == 1
        assert events[0].status == MoveStatus.OK
        assert events[0].observed.switch_name == "switch1"

    def test_multiple_servers(self, mock_settings):
        """Test correlation with multiple servers."""
        correlator = Correlator()

        servers = [
            make_server("srv1", "aa:bb:cc:dd:ee:01", "switch1", "Ethernet1"),
            make_server("srv2", "aa:bb:cc:dd:ee:02", "switch1", "Ethernet2"),
            make_server("srv3", "aa:bb:cc:dd:ee:03", "switch1", "Ethernet3"),
        ]
        fdb = {
            "switch1": make_fdb(
                "switch1",
                [
                    ("aa:bb:cc:dd:ee:01", "Ethernet1", 100),  # OK
                    ("aa:bb:cc:dd:ee:02", "Ethernet5", 100),  # MOVE
                    # srv3 MAC missing - NOT_FOUND
                ],
            ),
        }

        events = correlator.correlate(servers, fdb)

        assert len(events) == 3
        statuses = {e.server.server_name: e.status for e in events}
        assert statuses["srv1"] == MoveStatus.OK
        assert statuses["srv2"] == MoveStatus.MOVE_DETECTED
        assert statuses["srv3"] == MoveStatus.NOT_FOUND
