"""NetBox API client for reading server IPMI information and cabling."""

from typing import Any

import pynetbox
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import Settings, get_settings
from .logging import get_logger
from .models import ExpectedEndpoint, IpmiInterface, ServerIpmi

logger = get_logger(__name__)


class NetBoxClient:
    """
    Read-only NetBox API client.

    This client only performs read operations - no modifications to NetBox.
    """

    def __init__(self, settings: Settings | None = None):
        """Initialize NetBox client."""
        self.settings = settings or get_settings()
        self._api: pynetbox.api | None = None

    @property
    def api(self) -> pynetbox.api:
        """Get or create pynetbox API instance."""
        if self._api is None:
            self._api = pynetbox.api(
                self.settings.netbox_url,
                token=self.settings.netbox_token,
            )
            if not self.settings.netbox_verify_ssl:
                # Disable SSL verification if configured
                import requests

                session = requests.Session()
                session.verify = False
                self._api.http_session = session
        return self._api

    def _normalize_mac(self, mac: str) -> str:
        """Normalize MAC address to lowercase with colons."""
        # Remove common separators and convert to lowercase
        mac = mac.lower().replace("-", "").replace(":", "").replace(".", "")
        # Insert colons
        return ":".join(mac[i : i + 2] for i in range(0, 12, 2))

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def get_servers_with_ipmi(self) -> list[ServerIpmi]:
        """
        Get all devices with OOB IP that have cabling in NetBox.

        Filters devices by presence of oob_ip field (out-of-band management IP).
        This is the standard NetBox field for IPMI/iLO/iDRAC addresses.

        Returns:
            List of ServerIpmi objects with expected endpoints.
        """
        logger.info("Fetching devices with OOB IP from NetBox")

        servers: list[ServerIpmi] = []

        # Get all devices that have oob_ip set
        # NetBox API: has_oob_ip=True filters devices with OOB IP assigned
        devices = list(self.api.dcim.devices.filter(has_oob_ip=True))
        logger.debug(f"Found {len(devices)} devices with OOB IP")

        for device in devices:
            # Get OOB IP address from device
            oob_ip = None
            if device.oob_ip:
                oob_ip = str(device.oob_ip.address).split("/")[0]

            # Find the interface that has the OOB IP assigned
            # This is the IPMI/iLO/iDRAC interface
            oob_interface = self._find_oob_interface(device)

            if oob_interface is None:
                logger.debug(
                    f"Could not find OOB interface for device",
                    device=device.name,
                )
                continue

            # Must have a MAC address
            if not oob_interface.mac_address:
                logger.debug(
                    f"OOB interface has no MAC address",
                    device=device.name,
                    interface=oob_interface.name,
                )
                continue

            # Build IpmiInterface
            ipmi_iface = IpmiInterface(
                device_id=device.id,
                device_name=device.name,
                interface_id=oob_interface.id,
                interface_name=oob_interface.name,
                mac_address=self._normalize_mac(oob_interface.mac_address),
                ip_address=oob_ip,
                netbox_url=f"{self.settings.netbox_url}/dcim/devices/{device.id}/",
            )

            # Get expected endpoint from cable
            expected = self._get_expected_endpoint(oob_interface)

            # Only include if there's a cable connection
            if expected is None:
                logger.debug(
                    f"OOB interface has no cable connection",
                    device=device.name,
                    interface=oob_interface.name,
                )
                continue

            servers.append(
                ServerIpmi(
                    interface=ipmi_iface,
                    expected_endpoint=expected,
                )
            )

        logger.info(f"Found {len(servers)} devices with connected OOB interfaces")
        return servers

    def _find_oob_interface(self, device: Any) -> Any | None:
        """
        Find the OOB (IPMI/iLO/iDRAC) interface for a device.

        Strategy:
        1. Find interface that has the device's oob_ip assigned
        2. Fallback: find interface with IPMI/iLO/iDRAC/BMC/OOB in name

        Args:
            device: pynetbox device object with oob_ip

        Returns:
            Interface object or None
        """
        if not device.oob_ip:
            return None

        # Get all interfaces for this device
        interfaces = list(self.api.dcim.interfaces.filter(device_id=device.id))

        # Strategy 1: Find interface with oob_ip assigned
        oob_ip_id = device.oob_ip.id
        for iface in interfaces:
            # Check if this interface has the OOB IP
            ip_addresses = list(self.api.ipam.ip_addresses.filter(interface_id=iface.id))
            for ip in ip_addresses:
                if ip.id == oob_ip_id:
                    return iface

        # Strategy 2: Fallback to name matching
        for iface in interfaces:
            iface_name = iface.name.upper() if iface.name else ""
            is_oob = any(
                pattern in iface_name
                for pattern in ["IPMI", "ILO", "IDRAC", "BMC", "OOB"]
            )
            if is_oob and iface.mac_address:
                return iface

        return None

    def _get_expected_endpoint(self, interface: Any) -> ExpectedEndpoint | None:
        """
        Get the expected switch endpoint from cable connection.

        Args:
            interface: pynetbox interface object

        Returns:
            ExpectedEndpoint or None if no cable connection
        """
        # Check if interface has a cable
        if not interface.cable:
            return None

        try:
            # Get the cable details
            cable = self.api.dcim.cables.get(interface.cable.id)
            if not cable:
                return None

            # Find the remote endpoint (not our interface)
            # Cables have a_terminations and b_terminations
            # In pynetbox, terminations are Interface objects directly
            remote_iface = None

            # Check a_terminations
            for term in cable.a_terminations or []:
                if term.id != interface.id:
                    remote_iface = term
                    break

            # Check b_terminations if not found
            if remote_iface is None:
                for term in cable.b_terminations or []:
                    if term.id != interface.id:
                        remote_iface = term
                        break

            if remote_iface is None:
                return None

            # Get full interface details if needed
            if not hasattr(remote_iface, 'device') or remote_iface.device is None:
                remote_iface = self.api.dcim.interfaces.get(remote_iface.id)
                if not remote_iface:
                    return None

            # Get the remote device (switch)
            remote_device = remote_iface.device
            if not remote_device:
                return None

            return ExpectedEndpoint(
                switch_id=remote_device.id,
                switch_name=remote_device.name,
                port_id=remote_iface.id,
                port_name=remote_iface.name,
                cable_id=cable.id,
                netbox_url=f"{self.settings.netbox_url}/dcim/devices/{remote_device.id}/",
            )

        except Exception as e:
            logger.warning(
                f"Error getting cable endpoint",
                interface=interface.name,
                error=str(e),
            )
            return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def get_switches(self) -> list[dict[str, Any]]:
        """
        Get all switches that should be queried for FDB.

        Returns:
            List of switch information dicts with name and management IP.
        """
        selector = self.settings.parse_selector(self.settings.switches_selector)
        logger.info("Fetching switches from NetBox", selector=selector)

        switches = []
        devices = list(self.api.dcim.devices.filter(**selector))

        for device in devices:
            # Get primary IP for SNMP queries
            primary_ip = None
            if device.primary_ip:
                primary_ip = str(device.primary_ip.address).split("/")[0]
            elif device.primary_ip4:
                primary_ip = str(device.primary_ip4.address).split("/")[0]

            if not primary_ip:
                logger.warning(
                    f"Switch has no primary IP, skipping",
                    switch=device.name,
                )
                continue

            switches.append(
                {
                    "id": device.id,
                    "name": device.name,
                    "ip": primary_ip,
                }
            )

        logger.info(f"Found {len(switches)} switches with primary IP")
        return switches

    # --- Tag Management ---

    def _ensure_tag_exists(self, tag_name: str, tag_slug: str | None = None) -> int:
        """
        Ensure a tag exists in NetBox, create if not.

        Args:
            tag_name: Display name of the tag
            tag_slug: URL-friendly slug (auto-generated if not provided)

        Returns:
            Tag ID
        """
        if tag_slug is None:
            tag_slug = tag_name.lower().replace(" ", "-")

        # Try to get existing tag
        existing = self.api.extras.tags.get(slug=tag_slug)
        if existing:
            return existing.id

        # Create new tag
        logger.info(f"Creating tag '{tag_name}' in NetBox")
        tag = self.api.extras.tags.create(
            name=tag_name,
            slug=tag_slug,
            color="f44336",  # Red color for alert tag
            description="Auto-created by IPMI Move Auditor - indicates IPMI MAC moved",
        )
        return tag.id

    def add_tag_to_device(self, device_id: int, tag_name: str) -> bool:
        """
        Add a tag to a device.

        Args:
            device_id: NetBox device ID
            tag_name: Name of the tag to add

        Returns:
            True if tag was added (or already present)
        """
        try:
            # Ensure tag exists and get its ID
            tag_slug = tag_name.lower().replace(" ", "-")
            tag_id = self._ensure_tag_exists(tag_name, tag_slug)

            # Get device
            device = self.api.dcim.devices.get(device_id)
            if not device:
                logger.warning(f"Device not found", device_id=device_id)
                return False

            # Get current tag IDs
            current_tag_ids = [t.id for t in device.tags] if device.tags else []

            # Check if tag already present
            if tag_id in current_tag_ids:
                logger.debug(f"Tag already present", device=device.name, tag=tag_name)
                return True

            # Add tag by ID
            current_tag_ids.append(tag_id)
            device.tags = current_tag_ids
            device.save()

            logger.info(f"Added tag to device", device=device.name, tag=tag_name)
            return True

        except Exception as e:
            logger.error(f"Failed to add tag", device_id=device_id, tag=tag_name, error=str(e))
            return False

    def remove_tag_from_device(self, device_id: int, tag_name: str) -> bool:
        """
        Remove a tag from a device.

        Args:
            device_id: NetBox device ID
            tag_name: Name of the tag to remove

        Returns:
            True if tag was removed (or wasn't present)
        """
        try:
            tag_slug = tag_name.lower().replace(" ", "-")

            # Get device
            device = self.api.dcim.devices.get(device_id)
            if not device:
                logger.warning(f"Device not found", device_id=device_id)
                return False

            # Get current tags as dict {slug: id}
            current_tags = {t.slug: t.id for t in device.tags} if device.tags else {}

            # Check if tag present
            if tag_slug not in current_tags:
                return True  # Already not present

            # Remove tag by rebuilding list without it
            new_tag_ids = [tid for slug, tid in current_tags.items() if slug != tag_slug]
            device.tags = new_tag_ids
            device.save()

            logger.info(f"Removed tag from device", device=device.name, tag=tag_name)
            return True

        except Exception as e:
            logger.error(f"Failed to remove tag", device_id=device_id, tag=tag_name, error=str(e))
            return False
