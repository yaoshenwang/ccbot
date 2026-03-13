"""Application entry point — CLI dispatcher and bot bootstrap.

Handles three execution modes:
  1. `ccbot hook` — delegates to hook.hook_main() for Claude Code hook processing.
  2. `ccbot dev` — watches src/ for changes and auto-restarts the bot (dev workflow).
     Also monitors subprocess exit — crashes, Conflict exits, and /restart all
     trigger automatic re-launch with a 2-second cooldown.
  3. Default — configures logging, initializes tmux session, and starts the
     Telegram bot polling loop via bot.create_bot().
"""

import logging
import sys


def _run_bot() -> None:
    """Start the bot process (used by both `run` and `dev` modes)."""
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.WARNING,
    )

    try:
        from .config import config
    except ValueError as e:
        from .utils import ccbot_dir

        config_dir = ccbot_dir()
        env_path = config_dir / ".env"
        print(f"Error: {e}\n")
        print(f"Create {env_path} with the following content:\n")
        print("  TELEGRAM_BOT_TOKEN=your_bot_token_here")
        print("  ALLOWED_USERS=your_telegram_user_id")
        print()
        print("Get your bot token from @BotFather on Telegram.")
        print("Get your user ID from @userinfobot on Telegram.")
        sys.exit(1)

    logging.getLogger("ccbot").setLevel(logging.DEBUG)
    logging.getLogger("telegram.ext.AIORateLimiter").setLevel(logging.INFO)
    logger = logging.getLogger(__name__)

    from .tmux_manager import tmux_manager

    logger.info("Allowed users: %s", config.allowed_users)
    logger.info("Claude projects path: %s", config.claude_projects_path)

    session = tmux_manager.get_or_create_session()
    logger.info("Tmux session '%s' ready", session.session_name)

    logger.info("Starting Telegram bot...")
    from .bot import create_bot

    application = create_bot()
    application.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
        bootstrap_retries=-1,
    )


def _dev_mode() -> None:
    """Watch src/ for changes AND monitor subprocess exit.

    Restarts the bot on:
      - File changes in src/ (original behavior)
      - Subprocess exit (crash, Conflict, /restart)
    Ctrl+C cleanly exits without restart.
    """
    import signal
    import subprocess
    import time
    from pathlib import Path

    from watchfiles import watch

    src_dir = Path(__file__).resolve().parent.parent
    print(f"[dev] Watching {src_dir} for changes...")

    proc: subprocess.Popen[bytes] | None = None

    def _start() -> subprocess.Popen[bytes]:
        return subprocess.Popen([sys.executable, "-m", "ccbot"])

    def _stop(p: subprocess.Popen[bytes]) -> None:
        if p.poll() is None:
            p.send_signal(signal.SIGTERM)
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    try:
        proc = _start()
        for changes in watch(
            src_dir,
            watch_filter=lambda _, path: path.endswith(".py"),
            rust_timeout=2000,
            yield_on_timeout=True,
        ):
            # Check if process exited (crash / /restart / Conflict)
            if proc.poll() is not None:
                rc = proc.returncode
                print(f"[dev] Process exited with code {rc}, restarting in 2s...")
                time.sleep(2)
                proc = _start()
                continue

            # File changes detected
            if changes:
                print("[dev] File changes detected, restarting...")
                _stop(proc)
                time.sleep(0.5)
                proc = _start()
    except KeyboardInterrupt:
        print("\n[dev] Ctrl+C received, shutting down...")
    finally:
        if proc and proc.poll() is None:
            _stop(proc)


def main() -> None:
    """Main entry point."""
    if len(sys.argv) > 1 and sys.argv[1] == "hook":
        from .hook import hook_main

        hook_main()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "version":
        from . import __version__

        print(f"ccbot {__version__}")
        return

    if len(sys.argv) > 1 and sys.argv[1] == "dev":
        _dev_mode()
        return

    _run_bot()


if __name__ == "__main__":
    main()
