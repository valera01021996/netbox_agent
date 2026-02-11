"""Tests for data models."""

from datetime import datetime

import pytest

from netbox_agent.models import (
    ExpectedEndpoint,
    FdbEntry,
    IpmiInterface,
    MoveEvent,
    MoveStatus,
    ObservedEndpoint,
    ServerIpmi,
    SwitchFdb,
)


class TestObservedEndpoint:
    """Tests for ObservedEndpoint."""

    def test_matches_exact(self):
        """Test exact match."""
        expected = ExpectedEndpoint(
            switch_id=1,
            switch_name="switch1",
            port_id=10,
            port_name="Ethernet1",
        )
        observed = ObservedEndpoint(
            switch_name="switch1",
            port_name="Ethernet1",
        )
        assert observed.matches(expected)

    def test_matches_case_insensitive(self):
        """Test case-insensitive match."""
        expected = ExpectedEndpoint(
            switch_id=1,
            switch_name="Switch1",
            port_id=10,
            port_name="ETHERNET1",
        )
        observed = ObservedEndpoint(
            switch_name="switch1",
            port_name="ethernet1",
        )
        assert observed.matches(expected)

    def test_no_match_different_switch(self):
        """Test no match for different switch."""
        expected = ExpectedEndpoint(
            switch_id=1,
            switch_name="switch1",
            port_id=10,
            port_name="Ethernet1",
        )
        observed = ObservedEndpoint(
            switch_name="switch2",
            port_name="Ethernet1",
        )
        assert not observed.matches(expected)

    def test_no_match_different_port(self):
        """Test no match for different port."""
        expected = ExpectedEndpoint(
            switch_id=1,
            switch_name="switch1",
            port_id=10,
            port_name="Ethernet1",
        )
        observed = ObservedEndpoint(
            switch_name="switch1",
            port_name="Ethernet2",
        )
        assert not observed.matches(expected)

    def test_matches_none_expected(self):
        """Test match returns False for None expected."""
        observed = ObservedEndpoint(
            switch_name="switch1",
            port_name="Ethernet1",
        )
        assert not observed.matches(None)


class TestServerIpmi:
    """Tests for ServerIpmi."""

    def test_mac_property(self):
        """Test MAC property returns interface MAC."""
        iface = IpmiInterface(
            device_id=1,
            device_name="server1",
            interface_id=10,
            interface_name="IPMI",
            mac_address="aa:bb:cc:dd:ee:ff",
        )
        server = ServerIpmi(interface=iface)
        assert server.mac == "aa:bb:cc:dd:ee:ff"

    def test_server_name_property(self):
        """Test server_name property returns device name."""
        iface = IpmiInterface(
            device_id=1,
            device_name="server1",
            interface_id=10,
            interface_name="IPMI",
            mac_address="aa:bb:cc:dd:ee:ff",
        )
        server = ServerIpmi(interface=iface)
        assert server.server_name == "server1"


class TestMoveStatus:
    """Tests for MoveStatus enum."""

    def test_status_values(self):
        """Test all status values exist."""
        assert MoveStatus.OK.value == "ok"
        assert MoveStatus.OK_MLAG_PEER.value == "ok_mlag_peer"
        assert MoveStatus.SUSPECT_UPLINK.value == "suspect_uplink"
        assert MoveStatus.MOVE_DETECTED.value == "move_detected"
        assert MoveStatus.MOVE_CONFIRMED.value == "move_confirmed"
        assert MoveStatus.NOT_FOUND.value == "not_found"
