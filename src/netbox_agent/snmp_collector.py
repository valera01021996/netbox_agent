"""SNMP FDB (MAC address table) collector."""

from datetime import datetime
from typing import Any

from pysnmp.hlapi import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    bulkCmd,
    nextCmd,
)
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import Settings, get_settings
from .logging import get_logger
from .models import FdbEntry, SwitchFdb

logger = get_logger(__name__)

# Standard MIB OIDs for FDB/MAC table
# dot1dTpFdbAddress - MAC addresses
DOT1D_TP_FDB_ADDRESS = "1.3.6.1.2.1.17.4.3.1.1"
# dot1dTpFdbPort - Port index
DOT1D_TP_FDB_PORT = "1.3.6.1.2.1.17.4.3.1.2"
# dot1dBasePortIfIndex - Map bridge port to ifIndex
DOT1D_BASE_PORT_IF_INDEX = "1.3.6.1.2.1.17.1.4.1.2"
# ifName - Interface name
IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
# ifDescr - Interface description (fallback)
IF_DESCR = "1.3.6.1.2.1.2.2.1.2"

# Q-BRIDGE-MIB for VLAN-aware FDB (dot1qTpFdbPort)
DOT1Q_TP_FDB_PORT = "1.3.6.1.2.1.17.7.1.2.2.1.2"


class SnmpCollector:
    """Collects FDB entries from switches using SNMP."""

    def __init__(self, settings: Settings | None = None):
        """Initialize SNMP collector."""
        self.settings = settings or get_settings()
        self._engine = SnmpEngine()

    def _normalize_mac(self, mac_bytes: bytes | str) -> str:
        """Normalize MAC address to lowercase with colons."""
        if isinstance(mac_bytes, bytes):
            mac = mac_bytes.hex()
        else:
            mac = str(mac_bytes).lower().replace("-", "").replace(":", "").replace(".", "")
        return ":".join(mac[i : i + 2] for i in range(0, 12, 2))

    def _get_snmp_transport(self, ip: str) -> UdpTransportTarget:
        """Create SNMP transport target."""
        return UdpTransportTarget(
            (ip, 161),
            timeout=self.settings.snmp_timeout,
            retries=self.settings.snmp_retries,
        )

    def _get_community(self) -> CommunityData:
        """Get SNMP community data."""
        return CommunityData(self.settings.snmp_community)

    def _walk_oid(self, ip: str, oid: str) -> dict[str, Any]:
        """
        Walk an SNMP OID tree and return results.

        Returns dict mapping OID suffix to value.
        """
        results = {}
        transport = self._get_snmp_transport(ip)
        community = self._get_community()

        for error_indication, error_status, error_index, var_binds in bulkCmd(
            self._engine,
            community,
            transport,
            ContextData(),
            0,  # nonRepeaters
            50,  # maxRepetitions
            ObjectType(ObjectIdentity(oid)),
            lexicographicMode=False,
        ):
            if error_indication:
                logger.warning(f"SNMP error: {error_indication}", ip=ip, oid=oid)
                break

            if error_status:
                logger.warning(
                    f"SNMP error status: {error_status.prettyPrint()} at {error_index}",
                    ip=ip,
                    oid=oid,
                )
                break

            for var_bind in var_binds:
                oid_str = str(var_bind[0])
                if not oid_str.startswith(oid):
                    continue
                suffix = oid_str[len(oid) + 1 :]  # +1 for the dot
                results[suffix] = var_bind[1]

        return results

    def _get_interface_names(self, ip: str) -> dict[int, str]:
        """Get mapping of ifIndex to interface name."""
        # Try ifName first (more reliable)
        names = self._walk_oid(ip, IF_NAME)
        result = {}

        for suffix, value in names.items():
            try:
                if_index = int(suffix)
                result[if_index] = str(value)
            except (ValueError, TypeError):
                continue

        # Fallback to ifDescr if ifName is empty
        if not result:
            descrs = self._walk_oid(ip, IF_DESCR)
            for suffix, value in descrs.items():
                try:
                    if_index = int(suffix)
                    result[if_index] = str(value)
                except (ValueError, TypeError):
                    continue

        return result

    def _get_bridge_port_mapping(self, ip: str) -> dict[int, int]:
        """Get mapping of bridge port index to ifIndex."""
        mapping = self._walk_oid(ip, DOT1D_BASE_PORT_IF_INDEX)
        result = {}

        for suffix, value in mapping.items():
            try:
                bridge_port = int(suffix)
                if_index = int(value)
                result[bridge_port] = if_index
            except (ValueError, TypeError):
                continue

        return result

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=2, max=5),
    )
    def collect_fdb(self, switch_name: str, switch_ip: str) -> SwitchFdb:
        """
        Collect FDB entries from a switch.

        Args:
            switch_name: Name of the switch
            switch_ip: IP address of the switch for SNMP queries

        Returns:
            SwitchFdb with collected entries or error
        """
        logger.debug(f"Collecting FDB from {switch_name}", ip=switch_ip)

        try:
            # Get interface names and bridge port mapping
            if_names = self._get_interface_names(switch_ip)
            bridge_ports = self._get_bridge_port_mapping(switch_ip)

            entries: list[FdbEntry] = []

            # Try Q-BRIDGE-MIB first (VLAN-aware)
            qbridge_fdb = self._walk_oid(switch_ip, DOT1Q_TP_FDB_PORT)

            if qbridge_fdb:
                # Q-BRIDGE format: VLAN.MAC -> port
                for suffix, port_value in qbridge_fdb.items():
                    try:
                        parts = suffix.split(".")
                        if len(parts) < 7:
                            continue

                        vlan = int(parts[0])
                        mac_parts = parts[1:7]
                        mac_addr = self._normalize_mac(
                            bytes([int(x) for x in mac_parts])
                        )

                        bridge_port = int(port_value)
                        if_index = bridge_ports.get(bridge_port, bridge_port)
                        port_name = if_names.get(if_index, f"port{bridge_port}")

                        entries.append(
                            FdbEntry(
                                mac_address=mac_addr,
                                port_name=port_name,
                                vlan=vlan,
                            )
                        )
                    except (ValueError, TypeError, IndexError) as e:
                        logger.debug(f"Error parsing Q-BRIDGE entry: {e}")
                        continue
            else:
                # Fallback to BRIDGE-MIB (non-VLAN-aware)
                fdb_ports = self._walk_oid(switch_ip, DOT1D_TP_FDB_PORT)
                fdb_macs = self._walk_oid(switch_ip, DOT1D_TP_FDB_ADDRESS)

                for suffix, port_value in fdb_ports.items():
                    try:
                        # Get MAC address for this entry
                        mac_raw = fdb_macs.get(suffix)
                        if not mac_raw:
                            continue

                        if hasattr(mac_raw, "prettyPrint"):
                            mac_hex = mac_raw.prettyPrint()
                            # Remove 0x prefix if present
                            if mac_hex.startswith("0x"):
                                mac_hex = mac_hex[2:]
                            mac_addr = self._normalize_mac(mac_hex)
                        else:
                            mac_addr = self._normalize_mac(bytes(mac_raw))

                        bridge_port = int(port_value)
                        if_index = bridge_ports.get(bridge_port, bridge_port)
                        port_name = if_names.get(if_index, f"port{bridge_port}")

                        entries.append(
                            FdbEntry(
                                mac_address=mac_addr,
                                port_name=port_name,
                                vlan=None,
                            )
                        )
                    except (ValueError, TypeError) as e:
                        logger.debug(f"Error parsing BRIDGE-MIB entry: {e}")
                        continue

            logger.info(
                f"Collected {len(entries)} FDB entries from {switch_name}",
                switch=switch_name,
            )

            return SwitchFdb(
                switch_name=switch_name,
                entries=entries,
                collected_at=datetime.utcnow(),
            )

        except Exception as e:
            logger.error(
                f"Failed to collect FDB from {switch_name}",
                switch=switch_name,
                error=str(e),
            )
            return SwitchFdb(
                switch_name=switch_name,
                entries=[],
                collected_at=datetime.utcnow(),
                error=str(e),
            )

    def collect_all(self, switches: list[dict[str, Any]]) -> dict[str, SwitchFdb]:
        """
        Collect FDB from all switches.

        Args:
            switches: List of switch dicts with 'name' and 'ip' keys

        Returns:
            Dict mapping switch name to SwitchFdb
        """
        results = {}
        for switch in switches:
            fdb = self.collect_fdb(switch["name"], switch["ip"])
            results[switch["name"]] = fdb
        return results
