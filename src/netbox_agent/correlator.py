"""Correlator for comparing expected vs observed MAC locations."""

import re
from datetime import datetime

from .config import Settings, get_settings
from .logging import get_logger
from .models import (
    MoveEvent,
    MoveStatus,
    ObservedEndpoint,
    ServerIpmi,
    SwitchFdb,
    normalize_port_name,
)

logger = get_logger(__name__)


class Correlator:
    """
    Compares expected MAC locations (from NetBox) with observed locations (from FDB).

    Handles:
    - Exact match detection
    - MLAG peer detection
    - Uplink/trunk noise filtering
    - Move detection
    """

    def __init__(self, settings: Settings | None = None):
        """Initialize correlator."""
        self.settings = settings or get_settings()
        self._uplink_ports = self.settings.get_uplink_ports()
        self._uplink_patterns = self.settings.get_uplink_patterns()
        self._mlag_groups = self.settings.get_mlag_groups()

        # Build reverse MLAG lookup: switch -> group members
        self._mlag_peers: dict[str, set[str]] = {}
        for group_name, members in self._mlag_groups.items():
            member_set = {m.lower() for m in members}
            for member in members:
                self._mlag_peers[member.lower()] = member_set

    def _is_uplink_port(self, port_name: str) -> bool:
        """Check if a port is an uplink/trunk port."""
        # Check explicit port list
        if port_name in self._uplink_ports:
            return True

        # Check patterns
        for pattern in self._uplink_patterns:
            if pattern.search(port_name):
                return True

        return False

    def _is_mlag_peer(self, switch1: str, switch2: str) -> bool:
        """Check if two switches are MLAG peers."""
        switch1_lower = switch1.lower()
        switch2_lower = switch2.lower()

        peers = self._mlag_peers.get(switch1_lower)
        if peers and switch2_lower in peers:
            return True

        return False

    def _find_mac_in_fdb(
        self, mac: str, fdb_data: dict[str, SwitchFdb]
    ) -> list[ObservedEndpoint]:
        """
        Find all occurrences of a MAC address in FDB data.

        Returns list of observed endpoints where the MAC was found.
        """
        mac_lower = mac.lower()
        results = []

        for switch_name, switch_fdb in fdb_data.items():
            if switch_fdb.error:
                continue

            for entry in switch_fdb.entries:
                if entry.mac_address.lower() == mac_lower:
                    results.append(
                        ObservedEndpoint(
                            switch_name=switch_name,
                            port_name=entry.port_name,
                            vlan=entry.vlan,
                            timestamp=switch_fdb.collected_at,
                        )
                    )

        return results

    def _select_best_observation(
        self,
        observations: list[ObservedEndpoint],
        expected_switch: str | None,
        expected_port: str | None = None,
    ) -> ObservedEndpoint | None:
        """
        Select the best observation from multiple FDB entries.

        Priority:
        1. Non-uplink ports
        2. If multiple ports on same switch — prefer NON-expected port
           (if MAC is on both old and new port, the new port is the real one)
        3. First found
        """
        if not observations:
            return None

        if len(observations) > 1:
            logger.info(
                f"MAC found on {len(observations)} ports",
                ports=[f"{o.switch_name}:{o.port_name}" for o in observations],
            )

        # Filter out uplink ports if there are non-uplink options
        non_uplink = [o for o in observations if not self._is_uplink_port(o.port_name)]
        candidates = non_uplink if non_uplink else observations

        # If only one candidate — return it
        if len(candidates) == 1:
            return candidates[0]

        # If MAC is on multiple ports of the expected switch,
        # prefer the port that does NOT match expected (it's the real/new location)
        if expected_switch and expected_port:
            expected_lower = expected_switch.lower()
            expected_port_norm = normalize_port_name(expected_port)
            on_expected_switch = [
                o for o in candidates if o.switch_name.lower() == expected_lower
            ]
            if len(on_expected_switch) > 1:
                # MAC on multiple ports — prefer the non-expected port
                non_expected_port = [
                    o for o in on_expected_switch
                    if normalize_port_name(o.port_name) != expected_port_norm
                ]
                if non_expected_port:
                    return non_expected_port[0]

        return candidates[0]

    def correlate(
        self,
        servers: list[ServerIpmi],
        fdb_data: dict[str, SwitchFdb],
    ) -> list[MoveEvent]:
        """
        Correlate server IPMI MACs with FDB observations.

        Args:
            servers: List of servers with IPMI interfaces and expected endpoints
            fdb_data: FDB data from all switches

        Returns:
            List of MoveEvent objects describing the status of each MAC
        """
        events = []

        for server in servers:
            mac = server.mac
            expected = server.expected_endpoint

            # Find MAC in FDB
            observations = self._find_mac_in_fdb(mac, fdb_data)

            logger.info(
                f"MAC lookup result",
                server=server.server_name,
                mac=mac,
                expected=f"{expected.switch_name}:{expected.port_name}" if expected else None,
                found_on=[f"{o.switch_name}:{o.port_name}" for o in observations],
                found_count=len(observations),
            )

            if not observations:
                # MAC not found in any FDB
                events.append(
                    MoveEvent(
                        server=server,
                        expected=expected,
                        observed=None,
                        status=MoveStatus.NOT_FOUND,
                    )
                )
                logger.debug(
                    f"MAC not found in FDB",
                    server=server.server_name,
                    mac=mac,
                )
                continue

            # Select best observation
            expected_switch = expected.switch_name if expected else None
            expected_port = expected.port_name if expected else None
            observed = self._select_best_observation(observations, expected_switch, expected_port)

            if observed is None:
                events.append(
                    MoveEvent(
                        server=server,
                        expected=expected,
                        observed=None,
                        status=MoveStatus.NOT_FOUND,
                    )
                )
                continue

            # Determine status
            status = self._determine_status(expected, observed)

            events.append(
                MoveEvent(
                    server=server,
                    expected=expected,
                    observed=observed,
                    status=status,
                )
            )

            if status == MoveStatus.OK:
                logger.debug(
                    f"MAC matches expected location",
                    server=server.server_name,
                    mac=mac,
                    switch=observed.switch_name,
                    port=observed.port_name,
                )
            elif status == MoveStatus.OK_MLAG_PEER:
                logger.debug(
                    f"MAC on MLAG peer (OK)",
                    server=server.server_name,
                    mac=mac,
                    expected_switch=expected.switch_name if expected else None,
                    observed_switch=observed.switch_name,
                )
            elif status == MoveStatus.SUSPECT_UPLINK:
                logger.info(
                    f"MAC found on uplink port (suspect)",
                    server=server.server_name,
                    mac=mac,
                    switch=observed.switch_name,
                    port=observed.port_name,
                )
            else:
                logger.warning(
                    f"MAC move detected",
                    server=server.server_name,
                    mac=mac,
                    expected_switch=expected.switch_name if expected else None,
                    expected_port=expected.port_name if expected else None,
                    observed_switch=observed.switch_name,
                    observed_port=observed.port_name,
                )

        return events

    def _determine_status(
        self,
        expected: "ExpectedEndpoint | None",
        observed: ObservedEndpoint,
    ) -> MoveStatus:
        """Determine the status of a MAC observation."""
        from .models import ExpectedEndpoint

        if expected is None:
            # No expected endpoint - can't compare
            return MoveStatus.MOVE_DETECTED

        # Check for exact match
        if observed.matches(expected):
            return MoveStatus.OK

        # Check if on MLAG peer (same port name, different switch in same MLAG group)
        if self._is_mlag_peer(expected.switch_name, observed.switch_name):
            if normalize_port_name(observed.port_name) == normalize_port_name(expected.port_name):
                return MoveStatus.OK_MLAG_PEER
            # Different port on MLAG peer - still a move
            # But could be normal for certain MLAG configurations

        # Check if on uplink port
        if self._is_uplink_port(observed.port_name):
            return MoveStatus.SUSPECT_UPLINK

        # MAC is on unexpected switch/port
        return MoveStatus.MOVE_DETECTED
