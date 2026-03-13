"""Automatic update detection and upgrade for CCBot.

Periodically checks the GitHub remote for newer commits on the current branch.
When a new version is detected, waits for all Claude sessions to become idle,
notifies users, performs the upgrade, and exits for supervisor restart.

Core components:
  - AutoUpdater: Background asyncio task that polls GitHub API
  - Version utility functions: extract_commit_hash, relative_time, fetch_branch_info
  - Module-level API: is_update_in_progress, start_auto_update, stop_auto_update
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

from telegram import Bot

from .config import config
from .handlers.message_queue import get_message_queue
from .session import session_manager
from .terminal_parser import parse_status_line
from .tmux_manager import tmux_manager

logger = logging.getLogger(__name__)

# GitHub repo for version checks
GITHUB_REPO = "yaoshenwang/ccbot"

# Polling intervals (seconds)
CHECK_INTERVAL = 120  # Remote check interval
IDLE_CHECK_INTERVAL = 5  # Idle check interval when update pending


def extract_commit_hash(version: str) -> str | None:
    """Extract 7-char commit hash from version string like '0.1.dev89+g7a7d613.d20260313'."""
    if "+g" in version:
        raw = version.split("+g")[-1]
        # Strip dirty date suffix (.dYYYYMMDD)
        hash_part = raw.split(".")[0]
        return hash_part[:7]
    return None


def relative_time(iso_time: str) -> str:
    """Convert ISO 8601 timestamp to Chinese relative time string."""
    dt = datetime.fromisoformat(iso_time.replace("Z", "+00:00"))
    delta = datetime.now(timezone.utc) - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}秒前"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}分钟前"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}小时前"
    days = hours // 24
    return f"{days}天前"


async def fetch_branch_info(
    client: httpx.AsyncClient, branch: str
) -> tuple[str, str, str] | None:
    """Fetch latest commit info for a branch. Returns (sha7, relative_time, message)."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/commits/{branch}"
    try:
        resp = await client.get(url, timeout=5.0)
        if resp.status_code != 200:
            return None
        data = resp.json()
        sha7 = data["sha"][:7]
        commit_date = data["commit"]["committer"]["date"]
        message = data["commit"]["message"].split("\n")[0]
        return sha7, relative_time(commit_date), message
    except Exception:
        return None


def _is_dev_mode() -> bool:
    """Check if running from a git checkout (dev mode) vs installed package (production)."""
    return (Path(__file__).resolve().parent.parent.parent / ".git").is_dir()


def _detect_branch() -> str | None:
    """Detect the current branch. Returns None if detection fails (disables auto-update)."""
    # 1. Explicit env var
    env_branch = config.ccbot_branch
    if env_branch:
        logger.info("Auto-update branch from CCBOT_BRANCH: %s", env_branch)
        return env_branch

    # 2. Git rev-parse (works in dev mode)
    if _is_dev_mode():
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=Path(__file__).resolve().parent.parent.parent,
            )
            if result.returncode == 0:
                branch = result.stdout.strip()
                if branch and branch != "HEAD":
                    logger.info("Auto-update branch from git: %s", branch)
                    return branch
        except Exception:
            pass

    # 3. Match local hash against remote branches
    from . import __version__

    local_hash = extract_commit_hash(__version__)
    if local_hash:
        import httpx

        try:
            with httpx.Client() as client:
                for branch in ("main", "dev"):
                    url = f"https://api.github.com/repos/{GITHUB_REPO}/commits/{branch}"
                    resp = client.get(url, timeout=5.0)
                    if resp.status_code == 200:
                        remote_hash = resp.json()["sha"][:7]
                        if remote_hash == local_hash:
                            logger.info(
                                "Auto-update branch matched by hash: %s", branch
                            )
                            return branch
        except Exception:
            pass

    logger.warning("Could not detect branch — auto-update disabled")
    return None


def _get_local_hash() -> str | None:
    """Get the current local commit hash.

    In dev mode, reads directly from git (always accurate after git reset).
    In production, falls back to extracting from __version__.
    """
    if _is_dev_mode():
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short=7", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=Path(__file__).resolve().parent.parent.parent,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass

    from . import __version__

    return extract_commit_hash(__version__)


class AutoUpdater:
    """Background task that checks for remote updates and performs upgrades."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot
        self._branch: str | None = None
        self._local_hash: str | None = None
        self._update_pending = False
        self._update_in_progress = False
        self._task: asyncio.Task[None] | None = None

    @property
    def update_in_progress(self) -> bool:
        return self._update_in_progress

    async def start(self) -> asyncio.Task[None]:
        """Start the auto-update background task."""
        self._branch = _detect_branch()
        if self._branch is None:
            # Create a no-op task
            self._task = asyncio.create_task(asyncio.sleep(0))
            return self._task

        self._local_hash = _get_local_hash()
        logger.info(
            "Auto-update started: branch=%s, local_hash=%s, dev_mode=%s",
            self._branch,
            self._local_hash,
            _is_dev_mode(),
        )
        self._task = asyncio.create_task(self._run())
        return self._task

    def stop(self) -> None:
        """Cancel the auto-update task."""
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run(self) -> None:
        """Main loop: check remote, wait for idle, then upgrade."""
        try:
            while True:
                if not self._update_pending:
                    await asyncio.sleep(CHECK_INTERVAL)
                    await self._check_remote()
                else:
                    await asyncio.sleep(IDLE_CHECK_INTERVAL)
                    if await self._is_idle():
                        await self._perform_upgrade()
                        return  # os._exit called in _perform_upgrade
        except asyncio.CancelledError:
            logger.info("Auto-update task cancelled")
        except Exception:
            logger.exception("Auto-update task failed")

    async def _check_remote(self) -> None:
        """Check if remote has a newer commit."""
        if not self._branch:
            return

        import httpx

        async with httpx.AsyncClient() as client:
            info = await fetch_branch_info(client, self._branch)

        if info is None:
            logger.debug("Failed to fetch remote branch info")
            return

        remote_hash, _, _ = info
        if self._local_hash and remote_hash != self._local_hash:
            logger.info(
                "Update available: local=%s, remote=%s",
                self._local_hash,
                remote_hash,
            )
            self._update_pending = True
            self._remote_info = info

    async def _is_idle(self) -> bool:
        """Check if all Claude sessions are idle (no active work)."""
        for user_id, _thread_id, window_id in session_manager.iter_thread_bindings():
            # Check message queue
            queue = get_message_queue(user_id)
            if queue is not None and not queue.empty():
                return False

            # Check terminal status line
            pane_text = await tmux_manager.capture_pane(window_id)
            if pane_text and parse_status_line(pane_text) is not None:
                return False

        return True

    async def _notify_users(self, message: str) -> None:
        """Send update notification to all bound topics.

        Stale bindings (closed/deleted topics) are cleaned up on failure.
        """
        # Snapshot to avoid modifying during iteration
        bindings = list(session_manager.iter_thread_bindings())
        notified: set[tuple[int, int]] = set()
        for user_id, thread_id, _window_id in bindings:
            key = (user_id, thread_id)
            if key in notified:
                continue
            notified.add(key)

            chat_id = session_manager.resolve_chat_id(user_id, thread_id)
            try:
                # Use bot.send_message directly (not safe_send which swallows errors)
                await self._bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    message_thread_id=thread_id,
                )
            except Exception as e:
                err_msg = str(e).lower()
                if "thread not found" in err_msg or "chat not found" in err_msg:
                    logger.info(
                        "Cleaning stale binding: user=%d thread=%d",
                        user_id,
                        thread_id,
                    )
                    session_manager.unbind_thread(user_id, thread_id)
                else:
                    logger.warning(
                        "Failed to notify user=%d thread=%d: %s",
                        user_id,
                        thread_id,
                        e,
                    )

    async def _perform_upgrade(self) -> None:
        """Execute the upgrade: notify users, update code, exit."""
        self._update_in_progress = True

        remote_hash, _, commit_msg = self._remote_info
        await self._notify_users(f"⬆️ 升级中: {remote_hash} - {commit_msg}")
        await asyncio.sleep(1)  # Allow notification to be sent

        if _is_dev_mode():
            # Dev mode: git fetch + reset
            repo_root = str(Path(__file__).resolve().parent.parent.parent)
            logger.info("Dev mode upgrade: git fetch + reset in %s", repo_root)
            try:
                subprocess.run(
                    ["git", "fetch", "origin", self._branch or "dev"],
                    cwd=repo_root,
                    timeout=30,
                    check=True,
                )
                subprocess.run(
                    [
                        "git",
                        "reset",
                        "--hard",
                        f"origin/{self._branch or 'dev'}",
                    ],
                    cwd=repo_root,
                    timeout=10,
                    check=True,
                )
            except Exception:
                logger.exception("Git upgrade failed")
                self._update_in_progress = False
                self._update_pending = False
                return
        else:
            # Production: uv tool install
            logger.info("Production upgrade: uv tool install")
            try:
                subprocess.run(
                    [
                        "uv",
                        "tool",
                        "install",
                        f"git+https://github.com/{GITHUB_REPO}.git",
                        "--force",
                    ],
                    timeout=60,
                    check=True,
                )
            except Exception:
                logger.exception("uv tool install upgrade failed")
                self._update_in_progress = False
                self._update_pending = False
                return

        logger.info("Upgrade complete, exiting for restart")
        os._exit(0)


# --- Module-level API ---

_updater: AutoUpdater | None = None


def is_update_in_progress() -> bool:
    """Global gate: True when upgrade is executing (handlers should reject new messages)."""
    return _updater is not None and _updater.update_in_progress


async def start_auto_update(bot: Bot) -> asyncio.Task[None]:
    """Start the auto-update background task. Returns the task for lifecycle management."""
    global _updater
    _updater = AutoUpdater(bot)
    return await _updater.start()


def stop_auto_update() -> None:
    """Stop the auto-update background task."""
    global _updater
    if _updater:
        _updater.stop()
        _updater = None
