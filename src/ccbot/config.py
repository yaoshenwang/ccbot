"""Application configuration — reads env vars and exposes a singleton.

Loads TELEGRAM_BOT_TOKEN, ALLOWED_USERS, tmux/Claude paths, and
monitoring intervals from environment variables (with .env support).
.env loading priority: local .env (cwd) > $CCBOT_DIR/.env (default ~/.ccbot).
The module-level `config` instance is imported by nearly every other module.

Key class: Config (singleton instantiated as `config`).
"""

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from .utils import ccbot_dir

logger = logging.getLogger(__name__)

# Env vars that must not leak to child processes (e.g. Claude Code via tmux)
SENSITIVE_ENV_VARS = {"TELEGRAM_BOT_TOKEN", "ALLOWED_USERS", "OPENAI_API_KEY"}


class Config:
    """Application configuration loaded from environment variables."""

    def __init__(self) -> None:
        self.config_dir = ccbot_dir()
        self.config_dir.mkdir(parents=True, exist_ok=True)

        # Load .env: local (cwd) takes priority over config_dir
        # load_dotenv default override=False means first-loaded wins
        local_env = Path(".env")
        global_env = self.config_dir / ".env"
        if local_env.is_file():
            load_dotenv(local_env)
            logger.debug("Loaded env from %s", local_env.resolve())
        if global_env.is_file():
            load_dotenv(global_env)
            logger.debug("Loaded env from %s", global_env)

        self.telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN") or ""
        if not self.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

        allowed_users_str = os.getenv("ALLOWED_USERS", "")
        if not allowed_users_str:
            raise ValueError("ALLOWED_USERS environment variable is required")
        try:
            self.allowed_users: set[int] = {
                int(uid.strip()) for uid in allowed_users_str.split(",") if uid.strip()
            }
        except ValueError as e:
            raise ValueError(
                f"ALLOWED_USERS contains non-numeric value: {e}. "
                "Expected comma-separated Telegram user IDs."
            ) from e

        # Tmux session name and window naming
        self.tmux_session_name = os.getenv("TMUX_SESSION_NAME", "ccbot")
        self.tmux_main_window_name = "__main__"

        # State directory: isolated per instance when TMUX_SESSION_NAME is
        # non-default, so multiple bot instances do not clobber each other.
        if self.tmux_session_name != "ccbot":
            self.state_dir = self.config_dir / self.tmux_session_name
        else:
            self.state_dir = self.config_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # Claude command to run in new windows
        self.claude_command = os.getenv("CLAUDE_COMMAND", "claude")

        # All state files live under state_dir (isolated per instance)
        self.state_file = self.state_dir / "state.json"
        self.session_map_file = self.config_dir / "session_map.json"
        self.monitor_state_file = self.state_dir / "monitor_state.json"

        # Claude Code session monitoring configuration
        # Support custom projects path for Claude variants (e.g., cc-mirror, zai)
        # Priority: CCBOT_CLAUDE_PROJECTS_PATH > CLAUDE_CONFIG_DIR/projects > default
        custom_projects_path = os.getenv("CCBOT_CLAUDE_PROJECTS_PATH")
        claude_config_dir = os.getenv("CLAUDE_CONFIG_DIR")

        if custom_projects_path:
            self.claude_projects_path = Path(custom_projects_path)
        elif claude_config_dir:
            self.claude_projects_path = Path(claude_config_dir) / "projects"
        else:
            self.claude_projects_path = Path.home() / ".claude" / "projects"

        self.monitor_poll_interval = float(os.getenv("MONITOR_POLL_INTERVAL", "2.0"))

        # Display user messages in history and real-time notifications
        # When True, user messages are shown with a 👤 prefix
        self.show_user_messages = True

        # Show hidden (dot) directories in directory browser
        self.show_hidden_dirs = (
            os.getenv("CCBOT_SHOW_HIDDEN_DIRS", "").lower() == "true"
        )

        # Pinned directories for quick-start UI
        # Persisted in pinned_dirs.json; PINNED_DIRS env var seeds on first run
        self.pinned_dirs_file = self.config_dir / "pinned_dirs.json"
        self.pinned_dirs: list[str] = self._load_pinned_dirs()

        # Auto-update branch override (optional)
        self.ccbot_branch: str = os.getenv("CCBOT_BRANCH", "")

        # OpenAI API for voice message transcription (optional)
        self.openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
        self.openai_base_url: str = os.getenv(
            "OPENAI_BASE_URL", "https://api.openai.com/v1"
        )

        # Scrub sensitive vars from os.environ so child processes never inherit them.
        # Values are already captured in Config attributes above.
        for var in SENSITIVE_ENV_VARS:
            os.environ.pop(var, None)

        logger.debug(
            "Config initialized: dir=%s, token=%s..., allowed_users=%d, "
            "tmux_session=%s, claude_projects_path=%s",
            self.config_dir,
            self.telegram_bot_token[:8],
            len(self.allowed_users),
            self.tmux_session_name,
            self.claude_projects_path,
        )

    def is_user_allowed(self, user_id: int) -> bool:
        """Check if a user is in the allowed list."""
        return user_id in self.allowed_users

    def _load_pinned_dirs(self) -> list[str]:
        """Load pinned dirs from JSON file, seeding from env var if file doesn't exist."""
        if self.pinned_dirs_file.is_file():
            try:
                dirs = json.loads(self.pinned_dirs_file.read_text())
                if isinstance(dirs, list):
                    return [d for d in dirs if Path(d).is_dir()]
            except (json.JSONDecodeError, OSError):
                pass

        # Seed from PINNED_DIRS env var on first run
        pinned_dirs_str = os.getenv("PINNED_DIRS", "")
        dirs: list[str] = []
        if pinned_dirs_str:
            for d in pinned_dirs_str.split(","):
                expanded = str(Path(d.strip()).expanduser().resolve())
                if Path(expanded).is_dir():
                    dirs.append(expanded)
            if dirs:
                self._save_pinned_dirs(dirs)
        return dirs

    def _save_pinned_dirs(self, dirs: list[str]) -> None:
        """Persist pinned dirs list to JSON file."""
        try:
            self.pinned_dirs_file.write_text(json.dumps(dirs, indent=2) + "\n")
        except OSError as e:
            logger.error("Failed to save pinned dirs: %s", e)

    def add_pinned_dir(self, path: str) -> tuple[bool, str]:
        """Add a directory to pinned list. Returns (success, message)."""
        expanded = str(Path(path.strip()).expanduser().resolve())
        if not Path(expanded).is_dir():
            return False, f"目录不存在: `{path}`"
        if expanded in self.pinned_dirs:
            display = expanded.replace(str(Path.home()), "~")
            return False, f"已收藏: `{display}`"
        self.pinned_dirs.append(expanded)
        self._save_pinned_dirs(self.pinned_dirs)
        display = expanded.replace(str(Path.home()), "~")
        return True, f"✅ 已收藏: `{display}`"

    def remove_pinned_dir(self, name: str) -> tuple[bool, str]:
        """Remove a directory from pinned list by basename or full path.

        Returns (success, message).
        """
        # Try exact match first (full path or with ~ expansion)
        expanded = str(Path(name.strip()).expanduser().resolve())
        if expanded in self.pinned_dirs:
            self.pinned_dirs.remove(expanded)
            self._save_pinned_dirs(self.pinned_dirs)
            display = expanded.replace(str(Path.home()), "~")
            return True, f"✅ 已移除: `{display}`"

        # Try basename match
        matches = [d for d in self.pinned_dirs if Path(d).name == name.strip()]
        if len(matches) == 1:
            self.pinned_dirs.remove(matches[0])
            self._save_pinned_dirs(self.pinned_dirs)
            display = matches[0].replace(str(Path.home()), "~")
            return True, f"✅ 已移除: `{display}`"
        if len(matches) > 1:
            display = "\n".join(
                f"• `{m.replace(str(Path.home()), '~')}`" for m in matches
            )
            return False, f"多个匹配，请用完整路径:\n{display}"

        return False, f"未找到: `{name}`"


config = Config()
