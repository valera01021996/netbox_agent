"""NetBox Journal notification sender."""

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import Settings, get_settings
from .logging import get_logger
from .models import AlertInfo

logger = get_logger(__name__)


class NetBoxNotifier:
    """Sends notifications to NetBox Journal Entries."""

    def __init__(self, settings: Settings | None = None):
        """Initialize notifier."""
        self.settings = settings or get_settings()
        self._base_url = self.settings.netbox_url.rstrip("/")
        self._token = self.settings.netbox_token

    def _get_headers(self) -> dict[str, str]:
        """Get authorization headers."""
        return {
            "Authorization": f"Token {self._token}",
            "Content-Type": "application/json",
        }

    def _format_journal_entry(self, alert: AlertInfo) -> str:
        """Format alert as NetBox journal entry text."""
        if alert.is_reminder:
            prefix = "REMINDER: "
        else:
            prefix = ""

        lines = [
            f"**{prefix}IPMI Move Detected**",
            "",
            f"| Field | Value |",
            f"|:------|:------|",
            f"| IPMI MAC | `{alert.mac_address}` |",
            f"| IPMI IP | {alert.ip_address or 'N/A'} |",
            f"| Expected (NetBox) | {alert.expected_switch}:{alert.expected_port} |",
            f"| Observed (FDB) | {alert.observed_switch}:{alert.observed_port} |",
        ]

        if alert.observed_vlan:
            lines.append(f"| Observed VLAN | {alert.observed_vlan} |")

        lines.extend([
            f"| Consecutive Observations | {alert.consecutive_count} |",
            f"| First Detected | {alert.first_detected.strftime('%Y-%m-%d %H:%M UTC')} |",
            "",
            "---",
            "_Detected by NetBox IPMI Move Auditor_",
        ])

        return "\n".join(lines)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def _create_journal_entry(
        self,
        device_id: int,
        comments: str,
        kind: str = "warning",
    ) -> bool:
        """
        Create a journal entry for a device in NetBox.

        Args:
            device_id: NetBox device ID
            comments: Journal entry text (markdown supported)
            kind: Entry kind - info, success, warning, danger

        Returns:
            True if created successfully
        """
        url = f"{self._base_url}/api/extras/journal-entries/"
        payload = {
            "assigned_object_type": "dcim.device",
            "assigned_object_id": device_id,
            "kind": kind,
            "comments": comments,
        }

        response = requests.post(
            url,
            json=payload,
            headers=self._get_headers(),
            timeout=10,
            verify=self.settings.netbox_verify_ssl,
        )

        if response.status_code == 201:
            return True
        else:
            logger.error(
                "Failed to create NetBox journal entry",
                status_code=response.status_code,
                response=response.text,
                device_id=device_id,
            )
            return False

    def send_alert(self, alert: AlertInfo) -> bool:
        """
        Send alert to NetBox as journal entry.

        Args:
            alert: Alert information to send

        Returns:
            True if sent successfully, False otherwise
        """
        try:
            # Need device_id from alert
            # We'll extract it from server_url or need to pass it separately
            device_id = self._extract_device_id(alert.server_url)
            if device_id is None:
                logger.error(
                    "Cannot create journal entry: no device_id",
                    server=alert.server_name,
                )
                return False

            comments = self._format_journal_entry(alert)
            kind = "info" if alert.is_reminder else "warning"

            result = self._create_journal_entry(device_id, comments, kind)

            if result:
                logger.info(
                    "Journal entry created in NetBox",
                    server=alert.server_name,
                    device_id=device_id,
                    is_reminder=alert.is_reminder,
                )
            return result

        except Exception as e:
            logger.error(
                "Error creating NetBox journal entry",
                error=str(e),
            )
            raise

    def _extract_device_id(self, server_url: str | None) -> int | None:
        """Extract device ID from NetBox URL."""
        if not server_url:
            return None
        # URL format: https://netbox.example.com/dcim/devices/123/
        try:
            parts = server_url.rstrip("/").split("/")
            return int(parts[-1])
        except (ValueError, IndexError):
            return None

    def send_startup_notification(self) -> bool:
        """Send a startup notification (no-op for NetBox)."""
        logger.info("NetBox IPMI Move Auditor started")
        return True

    def send_error_notification(self, error_message: str) -> bool:
        """Send an error notification (logged only for NetBox)."""
        logger.error(f"Agent error: {error_message}")
        return True
