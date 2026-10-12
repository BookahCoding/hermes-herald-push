"""herald-push: registers the ``/herald-push`` command and the phone tools, and in the dashboard or
messaging gateway starts the watcher that pushes bot replies to the registered phones."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from . import push
from .commands import make_handler
from .phone import Phones, register_tools
from .store import Store
from .watcher import Watcher, is_long_lived_process

log = logging.getLogger(__name__)

VERSION = "0.4.0"

# One watcher per process, however many profiles load the plugin.
_watcher_lock = threading.Lock()
_watcher: Watcher | None = None


def _root() -> Path:
    """The Hermes root (not a profile's home): one identity and one phone list for every bot."""
    try:
        from hermes_constants import get_default_hermes_root
        return Path(get_default_hermes_root())
    except ImportError:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())


def register(ctx) -> None:
    global _watcher
    root = _root()
    store = Store(root / "plugin-data" / "herald-push")

    def watching() -> bool:
        return _watcher is not None

    def wake(device_id: str, request_id: str) -> None:
        """Tells a phone registered for push that a tool call waits for it."""
        device = store.get(device_id)
        if device is not None:
            _, _, sig = store.identity()
            push.publish(device, push.message("phone_request", request_id=request_id), sig)

    phones = Phones(store, wake)
    register_tools(ctx, phones)
    ctx.register_command("herald-push", make_handler(store, VERSION, watching, phones=phones),
                         description="Herald push notifications and phone tools: phones registered with this gateway",
                         args_hint="info | register <json> | unregister <id> | test <id> | phone …")

    if not is_long_lived_process():
        return
    with _watcher_lock:
        if _watcher is None:
            # One watcher for every profile; each bot's own "Send notifications" setting is read as it polls.
            _watcher = Watcher(root, store)
            _watcher.start()
            log.info("herald-push: watching Bot Chats and cron runs")
            started = _watcher

            def stop() -> None:
                global _watcher
                with _watcher_lock:
                    if _watcher is started:
                        _watcher.stop()
                        _watcher = None

            ctx.on_unload(stop)
