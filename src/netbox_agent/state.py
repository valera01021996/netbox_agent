"""State management with SQLite persistence."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Generator

from .config import Settings, get_settings
from .logging import get_logger
from .models import MoveEvent, MoveStatus, ObservedEndpoint

logger = get_logger(__name__)

# SQL schema
SCHEMA = """
CREATE TABLE IF NOT EXISTS mac_state (
    mac_address TEXT PRIMARY KEY,
    server_name TEXT NOT NULL,
    last_ok_seen_at TEXT,
    last_observed_switch TEXT,
    last_observed_port TEXT,
    last_observed_vlan INTEGER,
    move_counter INTEGER DEFAULT 0,
    first_move_seen_at TEXT,
    last_move_seen_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mac_address TEXT NOT NULL,
    alert_hash TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    observed_switch TEXT,
    observed_port TEXT,
    is_reminder INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_mac_state_mac ON mac_state(mac_address);
CREATE INDEX IF NOT EXISTS idx_alert_history_mac ON alert_history(mac_address);
CREATE INDEX IF NOT EXISTS idx_alert_history_hash ON alert_history(alert_hash);
"""


class StateManager:
    """Manages persistent state for move detection and alert deduplication."""

    def __init__(self, settings: Settings | None = None):
        """Initialize state manager."""
        self.settings = settings or get_settings()
        self._db_path = Path(self.settings.state_db_path)
        self._ensure_db()

    def _ensure_db(self) -> None:
        """Ensure database and schema exist."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._get_connection() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Get database connection context manager."""
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def update_state(self, event: MoveEvent) -> int:
        """
        Update state based on move event.

        Returns the new move counter for this MAC.
        """
        mac = event.server.mac
        server_name = event.server.server_name
        now = datetime.utcnow().isoformat()

        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Get current state
            cursor.execute(
                "SELECT * FROM mac_state WHERE mac_address = ?",
                (mac,),
            )
            row = cursor.fetchone()

            if event.status == MoveStatus.OK or event.status == MoveStatus.OK_MLAG_PEER:
                # MAC is in correct location - reset counter
                if row:
                    cursor.execute(
                        """
                        UPDATE mac_state
                        SET last_ok_seen_at = ?,
                            move_counter = 0,
                            first_move_seen_at = NULL,
                            last_move_seen_at = NULL,
                            updated_at = ?
                        WHERE mac_address = ?
                        """,
                        (now, now, mac),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO mac_state
                        (mac_address, server_name, last_ok_seen_at, move_counter, updated_at)
                        VALUES (?, ?, ?, 0, ?)
                        """,
                        (mac, server_name, now, now),
                    )
                conn.commit()
                return 0

            elif event.status in (MoveStatus.MOVE_DETECTED, MoveStatus.MOVE_CONFIRMED):
                observed = event.observed
                observed_switch = observed.switch_name if observed else None
                observed_port = observed.port_name if observed else None
                observed_vlan = observed.vlan if observed else None

                if row:
                    # Check if observed endpoint changed
                    prev_switch = row["last_observed_switch"]
                    prev_port = row["last_observed_port"]

                    if (
                        prev_switch == observed_switch
                        and prev_port == observed_port
                    ):
                        # Same endpoint - increment counter
                        new_counter = row["move_counter"] + 1
                        cursor.execute(
                            """
                            UPDATE mac_state
                            SET move_counter = ?,
                                last_move_seen_at = ?,
                                updated_at = ?
                            WHERE mac_address = ?
                            """,
                            (new_counter, now, now, mac),
                        )
                    else:
                        # Different endpoint - reset counter to 1
                        new_counter = 1
                        cursor.execute(
                            """
                            UPDATE mac_state
                            SET last_observed_switch = ?,
                                last_observed_port = ?,
                                last_observed_vlan = ?,
                                move_counter = 1,
                                first_move_seen_at = ?,
                                last_move_seen_at = ?,
                                updated_at = ?
                            WHERE mac_address = ?
                            """,
                            (
                                observed_switch,
                                observed_port,
                                observed_vlan,
                                now,
                                now,
                                now,
                                mac,
                            ),
                        )
                else:
                    # New entry
                    new_counter = 1
                    cursor.execute(
                        """
                        INSERT INTO mac_state
                        (mac_address, server_name, last_observed_switch, last_observed_port,
                         last_observed_vlan, move_counter, first_move_seen_at,
                         last_move_seen_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                        """,
                        (
                            mac,
                            server_name,
                            observed_switch,
                            observed_port,
                            observed_vlan,
                            now,
                            now,
                            now,
                        ),
                    )

                conn.commit()
                return new_counter if "new_counter" in dir() else 1

            elif event.status == MoveStatus.SUSPECT_UPLINK:
                # Don't count uplink observations
                return 0

            else:  # NOT_FOUND
                # Don't change counter when MAC not found
                if row:
                    return row["move_counter"]
                return 0

    def get_move_counter(self, mac: str) -> int:
        """Get current move counter for a MAC."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT move_counter FROM mac_state WHERE mac_address = ?",
                (mac,),
            )
            row = cursor.fetchone()
            return row["move_counter"] if row else 0

    def get_first_move_time(self, mac: str) -> datetime | None:
        """Get the timestamp of first move detection."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT first_move_seen_at FROM mac_state WHERE mac_address = ?",
                (mac,),
            )
            row = cursor.fetchone()
            if row and row["first_move_seen_at"]:
                return datetime.fromisoformat(row["first_move_seen_at"])
            return None

    def _compute_alert_hash(
        self,
        mac: str,
        observed_switch: str | None,
        observed_port: str | None,
    ) -> str:
        """Compute hash for alert deduplication."""
        data = f"{mac}:{observed_switch}:{observed_port}"
        return hashlib.sha256(data.encode()).hexdigest()[:16]

    def should_send_alert(
        self,
        mac: str,
        observed: ObservedEndpoint | None,
    ) -> tuple[bool, bool]:
        """
        Check if an alert should be sent.

        Returns:
            Tuple of (should_send, is_reminder)
        """
        observed_switch = observed.switch_name if observed else None
        observed_port = observed.port_name if observed else None
        alert_hash = self._compute_alert_hash(mac, observed_switch, observed_port)
        remind_after = self.settings.get_remind_after_timedelta()

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT sent_at, is_reminder FROM alert_history
                WHERE mac_address = ? AND alert_hash = ?
                ORDER BY sent_at DESC
                LIMIT 1
                """,
                (mac, alert_hash),
            )
            row = cursor.fetchone()

            if not row:
                # No previous alert for this situation
                return (True, False)

            last_sent = datetime.fromisoformat(row["sent_at"])
            now = datetime.utcnow()

            if now - last_sent > remind_after:
                # Enough time has passed - send reminder
                return (True, True)

            # Too soon to send another alert
            return (False, False)

    def record_alert(
        self,
        mac: str,
        observed: ObservedEndpoint | None,
        is_reminder: bool = False,
    ) -> None:
        """Record that an alert was sent."""
        observed_switch = observed.switch_name if observed else None
        observed_port = observed.port_name if observed else None
        alert_hash = self._compute_alert_hash(mac, observed_switch, observed_port)
        now = datetime.utcnow().isoformat()

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO alert_history
                (mac_address, alert_hash, sent_at, observed_switch, observed_port, is_reminder)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    mac,
                    alert_hash,
                    now,
                    observed_switch,
                    observed_port,
                    1 if is_reminder else 0,
                ),
            )
            conn.commit()

    def cleanup_old_alerts(self, days: int = 30) -> int:
        """Remove alert history older than N days."""
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM alert_history WHERE sent_at < ?",
                (cutoff,),
            )
            deleted = cursor.rowcount
            conn.commit()
            return deleted
