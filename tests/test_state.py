"""Tests for state management module."""

import os
import tempfile
from datetime import datetime, timedelta

import pytest

from netbox_agent.models import (
    ExpectedEndpoint,
    IpmiInterface,
    MoveEvent,
    MoveStatus,
    ObservedEndpoint,
    ServerIpmi,
)
from netbox_agent.state import StateManager


@pytest.fixture
def temp_db(monkeypatch):
    """Create a temporary database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    monkeypatch.setenv("NETBOX_URL", "https://netbox.example.com")
    monkeypatch.setenv("NETBOX_TOKEN", "test-token")
    monkeypatch.setenv("MM_WEBHOOK_URL", "https://mm.example.com/hooks/xxx")
    monkeypatch.setenv("STATE_DB_PATH", db_path)
    monkeypatch.setenv("REMIND_AFTER", "1h")

    yield db_path

    # Cleanup
    try:
        os.unlink(db_path)
    except OSError:
        pass


def make_event(
    mac: str,
    status: MoveStatus,
    observed_switch: str | None = None,
    observed_port: str | None = None,
) -> MoveEvent:
    """Create a test event."""
    iface = IpmiInterface(
        device_id=1,
        device_name="server1",
        interface_id=10,
        interface_name="IPMI",
        mac_address=mac,
    )
    expected = ExpectedEndpoint(
        switch_id=1,
        switch_name="switch1",
        port_id=10,
        port_name="Ethernet1",
    )
    observed = None
    if observed_switch and observed_port:
        observed = ObservedEndpoint(
            switch_name=observed_switch,
            port_name=observed_port,
        )

    return MoveEvent(
        server=ServerIpmi(interface=iface, expected_endpoint=expected),
        expected=expected,
        observed=observed,
        status=status,
    )


class TestStateManager:
    """Tests for StateManager class."""

    def test_update_state_ok_resets_counter(self, temp_db):
        """Test OK status resets move counter."""
        manager = StateManager()

        # First, create a move
        event = make_event(
            "aa:bb:cc:dd:ee:ff",
            MoveStatus.MOVE_DETECTED,
            "switch2",
            "Ethernet5",
        )
        counter = manager.update_state(event)
        assert counter == 1

        # Then, OK should reset it
        event = make_event("aa:bb:cc:dd:ee:ff", MoveStatus.OK)
        counter = manager.update_state(event)
        assert counter == 0

    def test_update_state_increments_counter(self, temp_db):
        """Test consecutive moves increment counter."""
        manager = StateManager()

        event = make_event(
            "aa:bb:cc:dd:ee:ff",
            MoveStatus.MOVE_DETECTED,
            "switch2",
            "Ethernet5",
        )

        counter1 = manager.update_state(event)
        assert counter1 == 1

        counter2 = manager.update_state(event)
        assert counter2 == 2

        counter3 = manager.update_state(event)
        assert counter3 == 3

    def test_update_state_different_endpoint_resets(self, temp_db):
        """Test different observed endpoint resets counter."""
        manager = StateManager()

        event1 = make_event(
            "aa:bb:cc:dd:ee:ff",
            MoveStatus.MOVE_DETECTED,
            "switch2",
            "Ethernet5",
        )
        manager.update_state(event1)
        manager.update_state(event1)
        assert manager.get_move_counter("aa:bb:cc:dd:ee:ff") == 2

        # Different endpoint
        event2 = make_event(
            "aa:bb:cc:dd:ee:ff",
            MoveStatus.MOVE_DETECTED,
            "switch3",
            "Ethernet10",
        )
        counter = manager.update_state(event2)
        assert counter == 1

    def test_should_send_alert_first_time(self, temp_db):
        """Test first alert should be sent."""
        manager = StateManager()

        observed = ObservedEndpoint(switch_name="switch2", port_name="Ethernet5")
        should_send, is_reminder = manager.should_send_alert(
            "aa:bb:cc:dd:ee:ff", observed
        )

        assert should_send is True
        assert is_reminder is False

    def test_should_send_alert_duplicate(self, temp_db):
        """Test duplicate alert should not be sent."""
        manager = StateManager()

        observed = ObservedEndpoint(switch_name="switch2", port_name="Ethernet5")

        # Record first alert
        manager.record_alert("aa:bb:cc:dd:ee:ff", observed)

        # Check if should send again
        should_send, is_reminder = manager.should_send_alert(
            "aa:bb:cc:dd:ee:ff", observed
        )

        assert should_send is False

    def test_get_first_move_time(self, temp_db):
        """Test getting first move time."""
        manager = StateManager()

        event = make_event(
            "aa:bb:cc:dd:ee:ff",
            MoveStatus.MOVE_DETECTED,
            "switch2",
            "Ethernet5",
        )
        manager.update_state(event)

        first_time = manager.get_first_move_time("aa:bb:cc:dd:ee:ff")
        assert first_time is not None
        assert isinstance(first_time, datetime)

    def test_cleanup_old_alerts(self, temp_db):
        """Test cleanup of old alert history."""
        manager = StateManager()

        observed = ObservedEndpoint(switch_name="switch2", port_name="Ethernet5")
        manager.record_alert("aa:bb:cc:dd:ee:ff", observed)

        # Cleanup (0 days = all)
        deleted = manager.cleanup_old_alerts(days=0)
        assert deleted == 1
