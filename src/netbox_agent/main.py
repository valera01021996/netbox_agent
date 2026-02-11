"""Main entry point for NetBox IPMI Move Auditor."""

import signal
import sys
import time
from datetime import datetime
from typing import NoReturn

from .config import get_settings
from .correlator import Correlator
from .logging import get_logger, setup_logging
from .models import AlertInfo, MoveEvent, MoveStatus
from .netbox_client import NetBoxClient
from .notifier import NetBoxNotifier
from .snmp_collector import SnmpCollector
from .state import StateManager

logger = get_logger(__name__)


class IpmiMoveAuditor:
    """Main agent that monitors IPMI MAC moves."""

    def __init__(self):
        """Initialize the auditor."""
        self.settings = get_settings()
        self.netbox = NetBoxClient(self.settings)
        self.snmp = SnmpCollector(self.settings)
        self.correlator = Correlator(self.settings)
        self.state = StateManager(self.settings)
        self.notifier = NetBoxNotifier(self.settings)
        self._running = True

    def _signal_handler(self, signum: int, frame) -> None:
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, shutting down...")
        self._running = False

    def _process_events(self, events: list[MoveEvent]) -> None:
        """Process correlation events and send alerts if needed."""
        confirm_threshold = self.settings.move_confirm_runs
        move_tag = self.settings.move_tag_name

        for event in events:
            # Update state and get counter
            counter = self.state.update_state(event)

            # Handle OK status - remove tag if present
            if event.status in (MoveStatus.OK, MoveStatus.OK_MLAG_PEER):
                # Remove move tag when server returns to expected location
                self.netbox.remove_tag_from_device(
                    event.server.interface.device_id,
                    move_tag,
                )
                continue

            # Only process MOVE_DETECTED events
            if event.status != MoveStatus.MOVE_DETECTED:
                continue

            # Update event with counter
            event.consecutive_count = counter

            # Check if move is confirmed (threshold reached)
            if counter < confirm_threshold:
                logger.info(
                    f"Move detected, waiting for confirmation",
                    server=event.server.server_name,
                    counter=counter,
                    threshold=confirm_threshold,
                    remaining=confirm_threshold - counter,
                )
                continue

            # Move is confirmed - mark as such
            event.status = MoveStatus.MOVE_CONFIRMED
            logger.warning(
                f"Move CONFIRMED after {counter} consecutive observations",
                server=event.server.server_name,
                counter=counter,
                expected=f"{event.expected.switch_name}:{event.expected.port_name}" if event.expected else "Unknown",
                observed=f"{event.observed.switch_name}:{event.observed.port_name}" if event.observed else "Unknown",
            )

            # Add move tag to device (triggers NetBox Webhook)
            self.netbox.add_tag_to_device(
                event.server.interface.device_id,
                move_tag,
            )

            # Check if we should send alert (deduplication)
            should_send, is_reminder = self.state.should_send_alert(
                event.server.mac,
                event.observed,
            )

            if not should_send:
                logger.debug(
                    f"Skipping alert (already sent recently)",
                    server=event.server.server_name,
                )
                continue

            # Build and send alert
            first_detected = self.state.get_first_move_time(event.server.mac)
            if first_detected is None:
                first_detected = datetime.utcnow()

            alert = AlertInfo(
                server_name=event.server.server_name,
                server_url=event.server.interface.netbox_url,
                mac_address=event.server.mac,
                ip_address=event.server.interface.ip_address,
                expected_switch=event.expected.switch_name if event.expected else "Unknown",
                expected_port=event.expected.port_name if event.expected else "Unknown",
                expected_url=event.expected.netbox_url if event.expected else None,
                observed_switch=event.observed.switch_name if event.observed else "Unknown",
                observed_port=event.observed.port_name if event.observed else "Unknown",
                observed_vlan=event.observed.vlan if event.observed else None,
                consecutive_count=counter,
                first_detected=first_detected,
                is_reminder=is_reminder,
            )

            try:
                if self.notifier.send_alert(alert):
                    self.state.record_alert(
                        event.server.mac,
                        event.observed,
                        is_reminder,
                    )
                    logger.info(
                        f"Alert sent for {event.server.server_name}",
                        is_reminder=is_reminder,
                    )
            except Exception as e:
                logger.error(
                    f"Failed to send alert",
                    server=event.server.server_name,
                    error=str(e),
                )

    def run_once(self) -> None:
        """Run a single poll cycle."""
        logger.info("Starting poll cycle")
        cycle_start = time.time()

        try:
            # Step 1: Get servers with IPMI from NetBox
            servers = self.netbox.get_servers_with_ipmi()
            if not servers:
                logger.warning("No servers with IPMI interfaces found")
                return

            # Step 2: Get switches for FDB collection
            switches = self.netbox.get_switches()
            if not switches:
                logger.warning("No switches found for FDB collection")
                return

            # Step 3: Collect FDB from all switches
            fdb_data = self.snmp.collect_all(switches)
            successful = sum(1 for fdb in fdb_data.values() if not fdb.error)
            logger.info(
                f"FDB collection complete",
                total_switches=len(switches),
                successful=successful,
            )

            # Step 4: Correlate expected vs observed
            events = self.correlator.correlate(servers, fdb_data)

            # Count by status
            status_counts = {}
            for event in events:
                status_counts[event.status.value] = status_counts.get(event.status.value, 0) + 1

            logger.info(
                f"Correlation complete",
                total_macs=len(events),
                status_counts=status_counts,
            )

            # Step 5: Process events and send alerts
            self._process_events(events)

        except Exception as e:
            logger.exception(f"Error in poll cycle: {e}")
            try:
                self.notifier.send_error_notification(str(e))
            except Exception:
                pass

        finally:
            cycle_time = time.time() - cycle_start
            logger.info(f"Poll cycle complete", duration_seconds=round(cycle_time, 2))

    def run(self) -> NoReturn:
        """Run the main agent loop."""
        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        logger.info(
            "Starting NetBox IPMI Move Auditor",
            poll_interval=self.settings.poll_interval,
            confirm_runs=self.settings.move_confirm_runs,
        )

        # Send startup notification
        self.notifier.send_startup_notification()

        # Cleanup old alerts
        deleted = self.state.cleanup_old_alerts()
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} old alert records")

        while self._running:
            try:
                self.run_once()

                # Sleep until next cycle
                if self._running:
                    logger.debug(f"Sleeping for {self.settings.poll_interval} seconds")
                    # Sleep in small increments to allow quick shutdown
                    for _ in range(self.settings.poll_interval):
                        if not self._running:
                            break
                        time.sleep(1)

            except Exception as e:
                logger.exception(f"Unexpected error: {e}")
                # Sleep before retry on error
                time.sleep(60)

        logger.info("Agent shutdown complete")
        sys.exit(0)


def main() -> None:
    """Entry point."""
    try:
        settings = get_settings()
        setup_logging(settings.log_level, settings.log_format)

        auditor = IpmiMoveAuditor()
        auditor.run()

    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
