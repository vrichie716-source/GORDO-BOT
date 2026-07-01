"""
Broadcaster Module — Automatic Message Sender via Telethon (MTProto)
═══════════════════════════════════════════════════════════════════════════════

Authenticates a Telegram user account and broadcasts messages to targeted
group sets (archived groups, folder-based groups) with comprehensive anti-ban
protections.

Architecture:
  - Uses Telethon's StringSession for portable, env-var-friendly persistence.
    The session token is a string stored as a Railway env var — free, no Volumes.
  - Integrates with python-telegram-bot (PTB) via shared asyncio event loop.
  - All anti-ban logic is documented inline for audit purposes.

Anti-Ban Strategy Overview:
  1. RANDOMIZED DELAYS — Each send waits base_delay ± variance% (e.g. 2min ± 30%)
     to avoid a perfectly periodic pattern that Telegram's servers detect.
  2. WARMUP PERIOD — The first N messages use 2× the normal delay so the account
     doesn't spike from idle to rapid-fire, which triggers anti-spam.
  3. MESSAGE VARIATION — Templates are rotated AND each send injects invisible
     Unicode chars so no two sends are byte-identical, defeating duplicate detection.
  4. FLOODWAIT HANDLING — When Telegram returns FloodWaitError(X), we sleep for
     X seconds PLUS a random buffer (5-30s) to avoid the exact-minimum pattern.
  5. PER-HOUR CAP — A hard limit on messages per rolling 60-minute window.
  6. PAUSE / RESUME — Mid-broadcast pause without losing progress.
  7. TEMPLATE MANAGEMENT — Enable/disable/add/remove templates at runtime.
  8. AUDIT LOGGING — Every send attempt, success, failure, skip, and FloodWait
     is logged to broadcast_log.jsonl with timestamps for compliance.
"""

import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.types import (
    DialogFilter,
    DialogFilterDefault,
    DialogFilterChatlist,
    InputPeerChannel,
    InputPeerChat,
    Channel,
    Chat,
)

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────
_DATA_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(_DATA_DIR, "broadcaster_config.yaml")
SESSION_FILE = os.path.join(_DATA_DIR, "broadcaster_session.txt")
CREDENTIALS_FILE = os.path.join(_DATA_DIR, "broadcaster_credentials.json")
LOG_FILE = os.path.join(_DATA_DIR, "broadcast_log.jsonl")


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class BroadcasterConfig:
    """All broadcaster settings, loaded from and saved to broadcaster_config.yaml."""

    # Target settings
    include_archived: bool = True
    target_folders: list[str] = field(default_factory=list)
    exceptions: list[int] = field(default_factory=list)

    # Message templates — each is a dict with 'text' and optional 'media'
    messages: list[dict] = field(default_factory=lambda: [
        {"text": "Hello! Check out our latest update 🚀", "media": None},
    ])

    # Timing
    delay_minutes: float = 2.0
    delay_variance: float = 0.3    # ±30% randomization

    # Rate limiting
    max_per_hour: int = 30

    # Anti-ban
    warmup_count: int = 5          # first N messages use 2× delay
    flood_wait_buffer_min: int = 5
    flood_wait_buffer_max: int = 30

    @classmethod
    def load(cls) -> "BroadcasterConfig":
        """Load config from YAML file, falling back to defaults if missing."""
        if not os.path.exists(CONFIG_FILE):
            logger.warning("No broadcaster_config.yaml found — using defaults.")
            return cls()
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return cls(
                include_archived=data.get("include_archived", True),
                target_folders=data.get("target_folders", []),
                exceptions=[int(x) for x in data.get("exceptions", [])],
                messages=data.get("messages") or [{"text": "Hello! 🚀", "media": None}],
                delay_minutes=float(data.get("delay_minutes", 2.0)),
                delay_variance=float(data.get("delay_variance", 0.3)),
                max_per_hour=int(data.get("max_per_hour", 30)),
                warmup_count=int(data.get("warmup_count", 5)),
                flood_wait_buffer_min=int(data.get("flood_wait_buffer_min", 5)),
                flood_wait_buffer_max=int(data.get("flood_wait_buffer_max", 30)),
            )
        except Exception as e:
            logger.error("Failed to load broadcaster config: %s — using defaults.", e)
            return cls()

    def save(self):
        """Write current config back to broadcaster_config.yaml."""
        data = {
            "include_archived": self.include_archived,
            "target_folders": self.target_folders,
            "exceptions": self.exceptions,
            "messages": self.messages,
            "delay_minutes": self.delay_minutes,
            "delay_variance": self.delay_variance,
            "max_per_hour": self.max_per_hour,
            "warmup_count": self.warmup_count,
            "flood_wait_buffer_min": self.flood_wait_buffer_min,
            "flood_wait_buffer_max": self.flood_wait_buffer_max,
        }
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
            logger.info("Config saved to %s.", CONFIG_FILE)
        except Exception as e:
            logger.error("Failed to save config: %s", e)

    def reload(self):
        """Reload config from file in-place."""
        fresh = BroadcasterConfig.load()
        for attr in vars(fresh):
            setattr(self, attr, getattr(fresh, attr))


# ══════════════════════════════════════════════════════════════════════════════
#  SESSION MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class SessionManager:
    """Manages Telethon StringSession and API credential persistence.

    All data is stored in broadcaster_credentials.json:
      {
        "api_id":  39767938,
        "api_hash": "21ae63ca...",
        "phone":   "+529811815398",
        "session": "<telethon_session_string>",
        "2fa_password": "<optional, stored for auto-reauth>"
      }

    The credentials.json file is gitignored and survives bot restarts.
    On Railway (ephemeral filesystem), the user re-runs /bot once after
    each redeploy — the interactive wizard re-creates the file in seconds.
    """

    @staticmethod
    def load() -> dict:
        """Load all credentials from file. Returns {} if not found."""
        if os.path.exists(CREDENTIALS_FILE):
            try:
                with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning("Failed to read credentials file: %s", e)
        return {}

    @staticmethod
    def save(api_id: int | str, api_hash: str, phone: str, session: str = ""):
        """Save credentials and session to file."""
        data = {
            "api_id": int(api_id),
            "api_hash": api_hash,
            "phone": phone,
            "session": session,
        }
        try:
            with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.info("Credentials saved to %s.", CREDENTIALS_FILE)
        except Exception as e:
            logger.error("Failed to save credentials: %s", e)

    @staticmethod
    def update_session(session_string: str):
        """Update only the session string in the credentials file."""
        creds = SessionManager.load()
        creds["session"] = session_string
        try:
            with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
                json.dump(creds, f, indent=2)
        except Exception as e:
            logger.error("Failed to update session in credentials file: %s", e)

    @staticmethod
    def has_credentials() -> bool:
        """True if api_id, api_hash, and phone are all stored."""
        c = SessionManager.load()
        return bool(c.get("api_id") and c.get("api_hash") and c.get("phone"))

    @staticmethod
    def load_session() -> str:
        """Get the stored session string (from creds file or env var)."""
        creds = SessionManager.load()
        if creds.get("session"):
            return creds["session"]
        # Fallback: legacy TG_SESSION env var
        session = os.environ.get("TG_SESSION", "").strip()
        if session:
            logger.info("Using session from TG_SESSION env var (legacy).")
        return session

    @staticmethod
    def update_2fa_password(password: str):
        """Store the 2FA password in the credentials file for auto-reauth."""
        creds = SessionManager.load()
        creds["2fa_password"] = password
        try:
            with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
                json.dump(creds, f, indent=2)
            logger.info("2FA password saved to credentials file.")
        except Exception as e:
            logger.error("Failed to save 2FA password: %s", e)

    @staticmethod
    def load_2fa_password() -> str:
        """Get the stored 2FA password, or empty string if not set."""
        creds = SessionManager.load()
        return creds.get("2fa_password", "")


# ══════════════════════════════════════════════════════════════════════════════
#  AUDIT LOGGER
# ══════════════════════════════════════════════════════════════════════════════

class AuditLogger:
    """Append-only JSONL logger for broadcast audit trail."""

    @staticmethod
    def log(event: str, chat_id: int = 0, chat_title: str = "",
            status: str = "ok", details: str = ""):
        """Append a single log entry to broadcast_log.jsonl."""
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "chat_id": chat_id,
            "chat_title": chat_title,
            "status": status,
            "details": details,
        }
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error("Failed to write audit log: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
#  RATE LIMITER
# ══════════════════════════════════════════════════════════════════════════════

class RateLimiter:
    """Sliding-window rate limiter for per-hour message caps."""

    def __init__(self, max_per_hour: int = 30):
        self.max_per_hour = max_per_hour
        self._timestamps: list[float] = []

    def record_send(self):
        self._timestamps.append(time.time())

    def can_send(self) -> bool:
        self._prune()
        return len(self._timestamps) < self.max_per_hour

    def seconds_until_available(self) -> float:
        self._prune()
        if len(self._timestamps) < self.max_per_hour:
            return 0.0
        oldest = self._timestamps[0]
        return max(0.0, (oldest + 3600) - time.time())

    def current_count(self) -> int:
        self._prune()
        return len(self._timestamps)

    def _prune(self):
        cutoff = time.time() - 3600
        self._timestamps = [t for t in self._timestamps if t > cutoff]


# ══════════════════════════════════════════════════════════════════════════════
#  MESSAGE TEMPLATE ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class MessageEngine:
    """Manages message template rotation and content variation.

    Anti-Ban Logic:
    - Templates are rotated in sequence (round-robin), skipping disabled ones.
    - Each send gets invisible Unicode chars injected at random positions so
      no two sends are byte-identical, defeating Telegram's duplicate detection.
    """

    _INVISIBLE_CHARS = [
        "\u200b",  # zero-width space
        "\u200c",  # zero-width non-joiner
        "\u200d",  # zero-width joiner
        "\ufeff",  # zero-width no-break space
    ]

    def __init__(self, templates: list[dict], disabled_indices: set[int] | None = None):
        self._templates = templates if templates else [{"text": "Hello!", "media": None}]
        self._disabled = disabled_indices or set()
        self._index = 0

    def next_message(self) -> dict | None:
        """Get next enabled template in rotation. Returns None if all disabled."""
        attempts = 0
        while attempts < len(self._templates):
            idx = self._index % len(self._templates)
            self._index += 1
            attempts += 1
            if idx in self._disabled:
                continue
            template = self._templates[idx]
            return {
                "text": self._randomize_text(template.get("text", "")),
                "media": template.get("media"),
            }
        return None  # All templates disabled

    def _randomize_text(self, text: str) -> str:
        """Insert 1-3 invisible chars at random positions to create byte-unique messages."""
        if not text:
            return text
        chars = list(text)
        for _ in range(random.randint(1, 3)):
            pos = random.randint(0, len(chars))
            chars.insert(pos, random.choice(self._INVISIBLE_CHARS))
        return "".join(chars)

    @property
    def active_count(self) -> int:
        return len(self._templates) - len(self._disabled)

    @property
    def total_count(self) -> int:
        return len(self._templates)


# ══════════════════════════════════════════════════════════════════════════════
#  BROADCASTER — Main Orchestrator
# ══════════════════════════════════════════════════════════════════════════════

class Broadcaster:
    """Main broadcaster engine.

    Manages Telethon client lifecycle, target discovery, pause/resume,
    per-template management, and the broadcast loop with anti-ban protections.
    """

    def __init__(self):
        self.client: Optional[TelegramClient] = None
        self.config: BroadcasterConfig = BroadcasterConfig.load()

        # Broadcast task control
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._pause_event = asyncio.Event()   # Set = paused, clear = running
        self._rate_limiter = RateLimiter(self.config.max_per_hour)

        # Per-template states: set of disabled template indices
        self._disabled_templates: set[int] = set()

        # Runtime stats (reset each run)
        self.stats = {
            "sent": 0,
            "failed": 0,
            "skipped": 0,
            "flood_waits": 0,
            "current_group": "",
            "current_index": 0,
            "total_targets": 0,
            "started_at": None,
            "finished_at": None,
        }

    # ── Client Lifecycle ─────────────────────────────────────────────────

    def _create_client(self, api_id=None, api_hash=None) -> TelegramClient:
        """Create a TelegramClient with StringSession for portability.

        Credential resolution order:
          1. Explicit parameters (used by setup wizard on first run)
          2. Credentials file (broadcaster_credentials.json — persistent)
          3. Environment variables (legacy fallback)
        """
        if not api_id or not api_hash:
            creds = SessionManager.load()
            api_id = api_id or creds.get("api_id") or os.environ.get("TG_API_ID", "").strip()
            api_hash = api_hash or creds.get("api_hash") or os.environ.get("TG_API_HASH", "").strip()
        if not api_id or not api_hash:
            raise ValueError(
                "Telegram API credentials not found. "
                "Run /bot to start the setup wizard."
            )
        session_str = SessionManager.load_session()
        self.client = TelegramClient(
            StringSession(session_str),
            int(api_id),
            api_hash,
            connection_retries=5,
            retry_delay=3,
            auto_reconnect=True,
        )
        return self.client

    async def connect(self):
        if not self.client:
            self._create_client()
        await self.client.connect()
        logger.info("Telethon client connected.")

    async def is_authenticated(self) -> bool:
        if not self.client:
            self._create_client()
        if not self.client.is_connected():
            await self.client.connect()
        return await self.client.is_user_authorized()

    async def send_code(self, phone=None) -> str:
        """Send login code. Phone resolution: param → credentials file → env var."""
        if not phone:
            creds = SessionManager.load()
            phone = creds.get("phone") or os.environ.get("TG_PHONE", "").strip()
        if not phone:
            raise ValueError(
                "Phone number not found. Run /bot to start the setup wizard."
            )
        if not self.client or not self.client.is_connected():
            await self.connect()
        result = await self.client.send_code_request(phone)
        logger.info("Login code sent to %s.", phone)
        return result.phone_code_hash

    async def sign_in_code(self, code: str, phone_code_hash: str) -> str:
        creds = SessionManager.load()
        phone = creds.get("phone") or os.environ.get("TG_PHONE", "").strip()
        try:
            await self.client.sign_in(phone, code, phone_code_hash=phone_code_hash)
            session_str = self.client.session.save()
            SessionManager.update_session(session_str)
            AuditLogger.log("auth_success", details="Signed in with code.")
            return "ok"
        except errors.SessionPasswordNeededError:
            return "2fa"

    async def sign_in_2fa(self, password: str):
        await self.client.sign_in(password=password)
        session_str = self.client.session.save()
        SessionManager.update_session(session_str)
        # Persist the 2FA password for automatic re-auth after redeployment
        SessionManager.update_2fa_password(password)
        AuditLogger.log("auth_success", details="Signed in with 2FA password.")
        logger.info("2FA sign-in successful.")

    async def disconnect(self):
        if self.client and self.client.is_connected():
            await self.client.disconnect()
            logger.info("Telethon client disconnected.")

    # ── Template Management ───────────────────────────────────────────────

    def disable_template(self, idx: int):
        """Pause (disable) a specific template by index."""
        self._disabled_templates.add(idx)
        AuditLogger.log("template_disable", details=f"Template {idx} disabled.")

    def enable_template(self, idx: int):
        """Resume (enable) a specific template by index."""
        self._disabled_templates.discard(idx)
        AuditLogger.log("template_enable", details=f"Template {idx} enabled.")

    def is_template_enabled(self, idx: int) -> bool:
        return idx not in self._disabled_templates

    def add_template(self, text: str, media: str | None = None):
        """Add a new message template and save to config."""
        self.config.messages.append({"text": text, "media": media})
        self.config.save()
        AuditLogger.log("template_add", details=f"Added template: {text[:60]}...")

    def remove_template(self, idx: int):
        """Permanently remove a template by index and save config."""
        if 0 <= idx < len(self.config.messages):
            removed = self.config.messages.pop(idx)
            # Adjust disabled set — remove deleted index, shift higher ones down
            new_disabled = set()
            for d in self._disabled_templates:
                if d < idx:
                    new_disabled.add(d)
                elif d > idx:
                    new_disabled.add(d - 1)
                # d == idx is dropped (it's deleted)
            self._disabled_templates = new_disabled
            self.config.save()
            AuditLogger.log("template_remove", details=f"Removed template {idx}: {str(removed)[:60]}")

    # ── Pause / Resume ────────────────────────────────────────────────────

    def pause_broadcast(self) -> str:
        """Pause the running broadcast (mid-loop). Does not stop it."""
        if not self.is_running:
            return "ℹ️ No broadcast is running."
        if self._pause_event.is_set():
            return "⏸ Broadcast is already paused."
        self._pause_event.set()
        AuditLogger.log("broadcast_pause")
        return "⏸ Broadcast paused."

    def resume_broadcast(self) -> str:
        """Resume a paused broadcast."""
        if not self.is_running:
            return "ℹ️ No broadcast is running."
        if not self._pause_event.is_set():
            return "▶️ Broadcast is not paused."
        self._pause_event.clear()
        AuditLogger.log("broadcast_resume")
        return "▶️ Broadcast resumed."

    @property
    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    # ── Target Discovery ─────────────────────────────────────────────────

    async def get_archived_groups(self) -> list[dict]:
        """Fetch all archived groups/supergroups from the user account."""
        groups = []
        async for dialog in self.client.iter_dialogs(archived=True):
            entity = dialog.entity
            if isinstance(entity, Channel) and entity.megagroup:
                groups.append({"id": dialog.id, "title": dialog.title or str(dialog.id), "type": "supergroup"})
            elif isinstance(entity, Chat):
                groups.append({"id": dialog.id, "title": dialog.title or str(dialog.id), "type": "group"})
        logger.info("Found %d archived groups.", len(groups))
        AuditLogger.log("discovery", details=f"Found {len(groups)} archived groups.")
        return groups

    async def get_folder_groups(self, folder_name: str) -> list[dict]:
        """Fetch all groups inside a specific Telegram folder."""
        groups = []
        try:
            result = await self.client(GetDialogFiltersRequest())
            filters_list = result.filters if hasattr(result, "filters") else result
            target_filter = None
            for f in filters_list:
                if isinstance(f, DialogFilterDefault):
                    continue
                title = getattr(f, "title", "")
                if hasattr(title, "text"):
                    title = title.text
                if title and title.lower() == folder_name.lower():
                    target_filter = f
                    break
            if not target_filter:
                logger.warning("Folder '%s' not found.", folder_name)
                return groups
            for peer in getattr(target_filter, "include_peers", []):
                try:
                    entity = await self.client.get_entity(peer)
                    if isinstance(entity, Channel) and entity.megagroup:
                        groups.append({"id": entity.id, "title": entity.title or str(entity.id), "type": "supergroup"})
                    elif isinstance(entity, Chat):
                        groups.append({"id": entity.id, "title": entity.title or str(entity.id), "type": "group"})
                except Exception as e:
                    logger.warning("Could not resolve peer %s: %s", peer, e)
        except Exception as e:
            logger.error("Failed to fetch folder '%s': %s", folder_name, e)
        logger.info("Found %d groups in folder '%s'.", len(groups), folder_name)
        return groups

    async def get_all_targets(self) -> list[dict]:
        """Gather all target groups from configured sources, minus exceptions."""
        seen_ids: set[int] = set()
        targets: list[dict] = []

        if self.config.include_archived:
            for g in await self.get_archived_groups():
                if g["id"] not in seen_ids:
                    seen_ids.add(g["id"])
                    targets.append(g)

        for folder_name in self.config.target_folders:
            for g in await self.get_folder_groups(folder_name):
                if g["id"] not in seen_ids:
                    seen_ids.add(g["id"])
                    targets.append(g)

        exception_set = set(self.config.exceptions)
        before = len(targets)
        targets = [g for g in targets if g["id"] not in exception_set]
        skipped = before - len(targets)
        if skipped:
            logger.info("Filtered out %d excepted groups.", skipped)
            AuditLogger.log("filter", details=f"Removed {skipped} exceptions. {len(targets)} remain.")

        return targets

    # ── Broadcast Loop ───────────────────────────────────────────────────

    async def start_broadcast(self, progress_callback=None) -> str:
        """Start the broadcast loop as a background asyncio task."""
        if self._task and not self._task.done():
            return "⚠️ A broadcast is already running."

        self.config.reload()
        self._rate_limiter = RateLimiter(self.config.max_per_hour)
        self._pause_event.clear()

        self.stats = {
            "sent": 0, "failed": 0, "skipped": 0, "flood_waits": 0,
            "current_group": "", "current_index": 0, "total_targets": 0,
            "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None,
        }

        self._stop_event.clear()
        self._task = asyncio.create_task(self._broadcast_loop(progress_callback))
        AuditLogger.log("broadcast_start")
        return "🚀 Broadcast started."

    async def stop_broadcast(self) -> str:
        """Gracefully stop the running broadcast."""
        if not self._task or self._task.done():
            return "ℹ️ No active broadcast to stop."
        self._pause_event.clear()   # Unblock if paused so it can see the stop signal
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=30)
        except asyncio.TimeoutError:
            self._task.cancel()
        self.stats["finished_at"] = datetime.now(timezone.utc).isoformat()
        AuditLogger.log("broadcast_stop", details=json.dumps(self.stats))
        return "⏹️ Broadcast stopped."

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _interruptible_sleep(self, seconds: float) -> bool:
        """Sleep for `seconds` while checking stop/pause signals every 1s.

        Returns True if sleep completed, False if stop was signalled.
        Automatically pauses mid-sleep when _pause_event is set.
        """
        remaining = seconds
        while remaining > 0:
            if self._stop_event.is_set():
                return False

            # ── Pause handling ──
            # When paused, we loop every 1s checking for resume/stop
            # instead of advancing the sleep countdown.
            while self._pause_event.is_set():
                if self._stop_event.is_set():
                    return False
                await asyncio.sleep(1.0)

            chunk = min(remaining, 1.0)
            await asyncio.sleep(chunk)
            remaining -= chunk

        return not self._stop_event.is_set()

    async def _broadcast_loop(self, progress_callback=None):
        """Core broadcast loop with all anti-ban protections.

        1. Discover target groups
        2. For each group:
           a. Check stop signal
           b. Handle pause (waits here until resumed)
           c. Check per-hour rate limit
           d. Calculate & wait randomized delay (with warmup)
           e. Pick next enabled message template (with variation)
           f. Send — catch FloodWait, permission errors, network errors
           g. Log everything to audit trail
        """
        try:
            # ── 1. Discover targets ──
            targets = await self.get_all_targets()
            if not targets:
                AuditLogger.log("broadcast_abort", details="No targets found.")
                if progress_callback:
                    await progress_callback("⚠️ No target groups found. Check your config.")
                return

            active_msgs = [i for i in range(len(self.config.messages)) if i not in self._disabled_templates]
            if not active_msgs:
                if progress_callback:
                    await progress_callback("⚠️ All message templates are disabled. Enable at least one.")
                return

            msg_engine = MessageEngine(self.config.messages, self._disabled_templates.copy())
            total = len(targets)
            base_delay_secs = self.config.delay_minutes * 60
            send_count = 0

            self.stats["total_targets"] = total

            if progress_callback:
                await progress_callback(
                    f"📡 <b>Broadcast started</b>\n\n"
                    f"🎯 Targets: <b>{total} groups</b>\n"
                    f"📝 Templates: <b>{msg_engine.active_count}/{msg_engine.total_count} active</b>\n"
                    f"⏱ Delay: <b>~{self.config.delay_minutes}min ±{int(self.config.delay_variance * 100)}%</b>\n"
                    f"🛡 Warmup: <b>first {self.config.warmup_count} msgs at 2× delay</b>\n"
                    f"📊 Cap: <b>{self.config.max_per_hour}/hour</b>"
                )

            # ── 2. Send loop ──
            for i, group in enumerate(targets):
                if self._stop_event.is_set():
                    break

                # ── 2a. Pause handling ──
                if self._pause_event.is_set():
                    if progress_callback:
                        await progress_callback(f"⏸ Broadcast paused at group {i+1}/{total}.")
                    while self._pause_event.is_set():
                        if self._stop_event.is_set():
                            break
                        await asyncio.sleep(1.0)
                    if self._stop_event.is_set():
                        break
                    if progress_callback:
                        await progress_callback(f"▶️ Broadcast resumed at group {i+1}/{total}.")

                chat_id = group["id"]
                chat_title = group["title"]
                self.stats["current_group"] = chat_title
                self.stats["current_index"] = i + 1

                # ── 2b. Rate limit check ──
                if not self._rate_limiter.can_send():
                    wait_time = self._rate_limiter.seconds_until_available()
                    logger.info("Rate limit reached. Waiting %.0fs.", wait_time)
                    AuditLogger.log("rate_limit_wait", chat_id=chat_id, status="waiting",
                                    details=f"Waiting {wait_time:.0f}s.")
                    if progress_callback:
                        await progress_callback(
                            f"⏳ Rate limit reached ({self.config.max_per_hour}/hr). "
                            f"Waiting {wait_time:.0f}s..."
                        )
                    if not await self._interruptible_sleep(wait_time):
                        break

                # ── 2c. Randomized delay (with warmup) ──
                # ANTI-BAN: No delay before the FIRST message.
                # All subsequent messages get base_delay ± variance%.
                # First warmup_count messages use 2× delay to ease in.
                if send_count > 0:
                    delay = base_delay_secs
                    variance = delay * self.config.delay_variance
                    randomized_delay = delay + random.uniform(-variance, variance)
                    if send_count < self.config.warmup_count:
                        randomized_delay *= 2.0  # WARMUP: 2× delay for first N sends
                    randomized_delay = max(randomized_delay, 10.0)  # Hard minimum 10s

                    logger.info("Waiting %.1fs before next send...", randomized_delay)
                    if not await self._interruptible_sleep(randomized_delay):
                        break

                if self._stop_event.is_set():
                    break

                # ── 2d. Pick next message template ──
                message = msg_engine.next_message()
                if message is None:
                    # All templates became disabled mid-run
                    AuditLogger.log("broadcast_abort", details="All templates disabled.")
                    if progress_callback:
                        await progress_callback("⚠️ All templates disabled. Broadcast stopped.")
                    break

                # ── 2e. Send ──
                try:
                    if message.get("media"):
                        await self.client.send_file(chat_id, message["media"],
                                                    caption=message.get("text", ""))
                    else:
                        await self.client.send_message(chat_id, message["text"])

                    send_count += 1
                    self.stats["sent"] += 1
                    self._rate_limiter.record_send()
                    AuditLogger.log("send_success", chat_id=chat_id, chat_title=chat_title,
                                    status="ok", details=f"Msg {send_count}/{total}.")
                    logger.info("[%d/%d] ✅ Sent to '%s'", i + 1, total, chat_title)

                    # Progress update every 5 sends
                    if progress_callback and send_count % 5 == 0:
                        await progress_callback(
                            f"📡 Progress: <b>{send_count}/{total}</b> sent\n"
                            f"❌ Failed: {self.stats['failed']} | ⏭ Skipped: {self.stats['skipped']}\n"
                            f"⏳ Flood waits: {self.stats['flood_waits']}"
                        )

                except errors.FloodWaitError as e:
                    # ANTI-BAN: Telegram demands we wait e.seconds.
                    # We add a random buffer so we don't look robotic.
                    self.stats["flood_waits"] += 1
                    buffer = random.randint(self.config.flood_wait_buffer_min,
                                           self.config.flood_wait_buffer_max)
                    total_wait = e.seconds + buffer
                    AuditLogger.log("flood_wait", chat_id=chat_id, chat_title=chat_title,
                                    status="paused",
                                    details=f"FloodWait {e.seconds}s + {buffer}s buffer.")
                    logger.warning("⏳ FloodWait: %ds required + %ds buffer", e.seconds, buffer)
                    if progress_callback:
                        await progress_callback(
                            f"⚠️ <b>FloodWait!</b> Telegram requires {e.seconds}s pause.\n"
                            f"Waiting {total_wait}s (includes {buffer}s safety buffer)..."
                        )
                    if not await self._interruptible_sleep(total_wait):
                        break
                    # Retry the same group
                    try:
                        if message.get("media"):
                            await self.client.send_file(chat_id, message["media"],
                                                        caption=message.get("text", ""))
                        else:
                            await self.client.send_message(chat_id, message["text"])
                        send_count += 1
                        self.stats["sent"] += 1
                        self._rate_limiter.record_send()
                        AuditLogger.log("send_success_retry", chat_id=chat_id,
                                        chat_title=chat_title, details="Sent after FloodWait.")
                    except Exception as retry_err:
                        self.stats["failed"] += 1
                        AuditLogger.log("send_fail_retry", chat_id=chat_id, status="error",
                                        details=str(retry_err))

                except (errors.ChatWriteForbiddenError, errors.UserBannedInChannelError,
                        errors.ChatAdminRequiredError):
                    self.stats["skipped"] += 1
                    AuditLogger.log("send_skip", chat_id=chat_id, chat_title=chat_title,
                                    status="skipped", details="No write permission.")
                    logger.warning("[%d/%d] ⚠️ No permission in '%s'", i + 1, total, chat_title)

                except errors.SlowModeWaitError as e:
                    self.stats["skipped"] += 1
                    AuditLogger.log("send_skip", chat_id=chat_id, chat_title=chat_title,
                                    status="skipped", details=f"Slow mode ({e.seconds}s).")

                except (ConnectionError, OSError) as e:
                    self.stats["failed"] += 1
                    AuditLogger.log("send_fail", chat_id=chat_id, status="error",
                                    details=f"Network error: {e}")
                    logger.error("Network error sending to '%s': %s", chat_title, e)
                    await asyncio.sleep(10)

                except Exception as e:
                    self.stats["failed"] += 1
                    AuditLogger.log("send_fail", chat_id=chat_id, chat_title=chat_title,
                                    status="error", details=str(e))
                    logger.error("[%d/%d] ❌ Failed to send to '%s': %s", i+1, total, chat_title, e)

            # ── Broadcast complete ──
            self.stats["finished_at"] = datetime.now(timezone.utc).isoformat()
            self.stats["current_group"] = ""
            summary = (
                f"✅ <b>Broadcast complete!</b>\n\n"
                f"📊 <b>Results:</b>\n"
                f"  ✅ Sent: <b>{self.stats['sent']}</b>\n"
                f"  ❌ Failed: <b>{self.stats['failed']}</b>\n"
                f"  ⏭ Skipped: <b>{self.stats['skipped']}</b>\n"
                f"  ⏳ Flood waits: <b>{self.stats['flood_waits']}</b>"
            )
            AuditLogger.log("broadcast_complete", details=json.dumps(self.stats))
            logger.info("Broadcast complete: %s", json.dumps(self.stats))
            if progress_callback:
                await progress_callback(summary)

        except asyncio.CancelledError:
            logger.info("Broadcast task cancelled.")
            AuditLogger.log("broadcast_cancelled")
        except Exception as e:
            logger.error("Broadcast loop crashed: %s", e, exc_info=True)
            AuditLogger.log("broadcast_crash", status="error", details=str(e))
            if progress_callback:
                await progress_callback(f"❌ Broadcast crashed: <code>{e}</code>")

    # ── Status / Display Helpers ──────────────────────────────────────────

    def get_status_text(self) -> str:
        """Build a rich HTML status summary."""
        if self.is_running:
            state = "⏸ PAUSED" if self.is_paused else "📡 RUNNING"
            elapsed = ""
            if self.stats.get("started_at"):
                start = datetime.fromisoformat(self.stats["started_at"])
                delta = datetime.now(timezone.utc) - start
                mins, secs = divmod(int(delta.total_seconds()), 60)
                elapsed = f"  ⏱ Running: <b>{mins}m {secs}s</b>\n"

            progress = ""
            if self.stats.get("total_targets"):
                pct = int(self.stats["current_index"] / self.stats["total_targets"] * 100)
                progress = (
                    f"  📈 Progress: <b>{self.stats['current_index']}/{self.stats['total_targets']}</b>"
                    f" ({pct}%)\n"
                )
                if self.stats.get("current_group"):
                    progress += f"  📍 Current: <i>{self.stats['current_group']}</i>\n"

            return (
                f"📡 <b>Status: {state}</b>\n"
                f"{'━' * 28}\n"
                f"{elapsed}"
                f"{progress}\n"
                f"  ✅ Sent: <b>{self.stats['sent']}</b>\n"
                f"  ❌ Failed: <b>{self.stats['failed']}</b>\n"
                f"  ⏭ Skipped: <b>{self.stats['skipped']}</b>\n"
                f"  ⏳ Flood waits: <b>{self.stats['flood_waits']}</b>\n"
                f"  📊 Rate: <b>{self._rate_limiter.current_count()}/{self.config.max_per_hour}</b>/hr"
            )
        else:
            last_run = ""
            if self.stats.get("finished_at"):
                last_run = f"\n  🕐 Last run: <i>{self.stats['finished_at'][:19].replace('T', ' ')} UTC</i>"
            return (
                f"📡 <b>Status: 💤 IDLE</b>\n"
                f"{'━' * 28}\n"
                f"  ✅ Last sent: <b>{self.stats['sent']}</b>\n"
                f"  ❌ Last failed: <b>{self.stats['failed']}</b>\n"
                f"  ⏭ Last skipped: <b>{self.stats['skipped']}</b>"
                f"{last_run}"
            )

    def get_config_text(self) -> str:
        """Build a rich HTML config summary."""
        folders = ", ".join(self.config.target_folders) if self.config.target_folders else "<i>None</i>"
        excepts = str(len(self.config.exceptions)) + " group(s)" if self.config.exceptions else "<i>None</i>"
        active = len(self.config.messages) - len(self._disabled_templates)

        return (
            f"⚙️ <b>Broadcaster Settings</b>\n"
            f"{'━' * 28}\n\n"
            f"🎯 <b>Targets:</b>\n"
            f"  {'✅' if self.config.include_archived else '❌'} Archived groups\n"
            f"  📁 Folders: {folders}\n"
            f"  🚫 Exceptions: {excepts}\n\n"
            f"📝 <b>Templates:</b> {active}/{len(self.config.messages)} active\n\n"
            f"⏱ <b>Timing:</b>\n"
            f"  Delay: <code>{self.config.delay_minutes} min</code>\n"
            f"  Variance: <code>±{int(self.config.delay_variance * 100)}%</code>\n"
            f"  Cap: <code>{self.config.max_per_hour}/hour</code>\n"
            f"  Warmup: <code>{self.config.warmup_count} msgs at 2×</code>\n\n"
            f"🛡 <b>FloodWait buffer:</b> {self.config.flood_wait_buffer_min}–{self.config.flood_wait_buffer_max}s"
        )

    def get_templates_text(self) -> str:
        """Build a summary of all message templates."""
        if not self.config.messages:
            return "📝 <b>No message templates configured.</b>"
        lines = [f"📝 <b>Message Templates ({len(self.config.messages)} total)</b>\n{'━' * 28}\n"]
        for i, msg in enumerate(self.config.messages):
            state = "✅" if i not in self._disabled_templates else "⏸"
            preview = msg.get("text", "")[:50].replace("<", "&lt;").replace(">", "&gt;")
            if len(msg.get("text", "")) > 50:
                preview += "..."
            media_tag = " 📎" if msg.get("media") else ""
            lines.append(f"{state} <b>#{i+1}</b>{media_tag}: <i>{preview}</i>")
        return "\n".join(lines)
