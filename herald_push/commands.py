"""``/herald-push``: how Herald registers a phone, over the dashboard's authenticated socket
(``command.dispatch``). Answers are JSON for Herald to read.

Slash commands can also be typed on a messaging platform (Telegram, Discord, ...); there they're refused,
or anyone allowed to chat with a bot could register their own phone and receive every bot's messages.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Callable, Optional

from . import cron, push
from .phone import TOOLS, Phones
from .store import PROFILE_RE, InvalidRegistration, Device, Store
from .watcher import profile_homes

# What this plugin can do that Herald checks for by name, not by version number.
FEATURES = ["profile_allowlist"]

log = logging.getLogger(__name__)

MAX_ARG = 8192
# A phone tool's answer (a calendar, a contact list) is bigger than anything else sent here.
MAX_ANSWER_ARG = 64000
USAGE = ("usage: /herald-push info | register <json> | unregister <device_id> | test <device_id> | "
         "phone tools <json> | phone pending <json> | phone answer <json> | phone here <json> | "
         "phone list <json> | phone order <json> | phone remove <json> | cron follow|following|inbox|read <json> | "
         "profiles get|set|clear|test <json>")


def trusted_surface() -> bool:
    """Only the dashboard socket (Herald, Desktop) may manage phones; fail closed when unsure."""
    if "tui_gateway.server" not in sys.modules and "tui_gateway" not in sys.modules:
        return False
    try:
        from gateway.session_context import session_is_messaging_surface
    except ImportError:
        return False
    try:
        return not session_is_messaging_surface()
    except Exception:
        return False


def phone_command(phones: Phones, raw: str, reply: Callable[..., str]) -> str:
    """``phone tools <json>``: what this phone has switched on. ``phone pending <json>``: the tool calls
    waiting for it. ``phone answer <json>``: the result of one. ``phone here <json>``: this phone sends messages
    on chat connection ``sid``, so calls from those turns come to it. ``phone list <json>``: every phone, in the
    order calls no phone started (cron jobs) try them. ``phone order <json>``: that order, as ``ids``. ``phone
    remove <json>``: forgets another phone, ``id``. Each carries the phone's ``device_id`` and secret ``key``."""
    op, _, rest = raw.partition(" ")
    try:
        data = json.loads(rest.strip())
    except ValueError:
        return reply(ok=False, error="not JSON")
    if not isinstance(data, dict):
        return reply(ok=False, error="not an object")
    device_id, key = data.get("device_id"), data.get("key")
    if not isinstance(device_id, str) or not isinstance(key, str):
        return reply(ok=False, error="bad device_id or key")
    if op == "tools":
        tools = data.get("tools")
        if not isinstance(tools, list):
            return reply(ok=False, error="tools must be a list")
        push_id = data.get("push_id")
        if push_id is not None and not isinstance(push_id, str):
            return reply(ok=False, error="bad push_id")
        try:
            kept = phones.report(device_id, key, str(data.get("name") or ""), tools, push_id)
        except ValueError as exc:
            return reply(ok=False, error=str(exc))
        return reply(ok=True, tools=kept)
    if not phones.verify(device_id, key):
        return reply(ok=False, error="unknown phone")
    if op == "here":
        sid = data.get("sid")
        if not isinstance(sid, str):
            return reply(ok=False, error="bad sid")
        return reply(ok=phones.here(device_id, sid))
    if op == "list":
        return reply(ok=True, phones=phones.listing(device_id))
    if op == "order":
        ids = data.get("ids")
        if not isinstance(ids, list) or not phones.order(ids):
            return reply(ok=False, error="ids must be a list of phone ids")
        return reply(ok=True, phones=phones.listing(device_id))
    if op == "remove":
        target = data.get("id")
        if not isinstance(target, str) or target == device_id:
            return reply(ok=False, error="Remove another phone; turn phone tools off to take this one away.")
        return reply(ok=phones.remove(target), phones=phones.listing(device_id))
    if op == "pending":
        # The gateway's time too: the phone measures each call's ``expires`` against it, not its own clock.
        return reply(ok=True, requests=phones.pending(device_id), now=int(phones.clock()))
    if op == "answer":
        error = data.get("error")
        if error is not None and not isinstance(error, str):
            return reply(ok=False, error="bad error")
        return reply(ok=phones.answer(device_id, str(data.get("id") or ""), data.get("result"), error))
    return reply(ok=False, error=USAGE)


def cron_command(store: Store, raw: str, reply: Callable[..., str], root: Optional[Path] = None) -> str:
    """``cron follow <json>``: the jobs a registered phone follows (``device_id``, ``jobs`` as job id to
    ``notify``, ``quiet`` or ``off``, ``default`` for every job not listed, ``failures``). ``cron following <json>``: what it follows now. ``cron inbox <json>``: the
    runs it follows, newest first (``limit``, ``before``). ``cron read <json>``: one run in full (``bot``, ``job``,
    ``run``). A phone's pushes are encrypted to it alone, so naming another phone's id only changes what that
    phone is sent; the runs themselves are the gateway's, which the dashboard can read anyway."""
    op, _, rest = raw.partition(" ")
    try:
        data = json.loads(rest.strip())
    except ValueError:
        return reply(ok=False, error="not JSON")
    if not isinstance(data, dict) or not isinstance(data.get("device_id"), str):
        return reply(ok=False, error="bad device_id")
    follows = cron.Follows(store)
    device_id = data["device_id"]
    # The runs a phone is shown are only those of the profiles it may hear from, as for its pushes.
    allow = store.allowlist()
    homes = [(name, home) for name, home in profile_homes(root if root is not None else store.root.parent.parent)
             if allow.allows(device_id, name)]
    if op == "inbox":
        limit, before = data.get("limit", 50), data.get("before", 0)
        if not isinstance(limit, int) or not isinstance(before, (int, float)):
            return reply(ok=False, error="bad limit or before")
        return reply(ok=True, runs=cron.inbox(homes, follows.get(device_id), limit, before))
    if op == "read":
        bot, job, run = data.get("bot"), data.get("job"), data.get("run")
        if not all(isinstance(v, str) for v in (bot, job, run)):
            return reply(ok=False, error="bad bot, job or run")
        found = cron.read(homes, bot, job, run)
        return reply(ok=True, run=found) if found else reply(ok=False, error="no such run")
    if op == "follow":
        try:
            follows.set(device_id, cron.Follow.parse(data))
        except ValueError as exc:
            return reply(ok=False, error=str(exc))
    elif op != "following":
        return reply(ok=False, error=USAGE)
    current = follows.get(device_id)
    return reply(ok=True, jobs=current.jobs, failures=current.failures, default=current.default)


def profiles_command(store: Store, raw: str, reply: Callable[..., str]) -> str:
    """``profiles get|set|clear|test <json>``: which profiles a registered phone may be sent. ``get`` reads the
    phone's list (``profiles`` is null when it has none and is sent everything); ``set`` replaces it with
    ``profiles``, a list of names, so a profile not on it (unknown, or made later) is not sent; ``clear`` goes back
    to no list; ``test`` sends the phone one synthetic Bot Chat message for ``profile`` through the same check
    as the real ones, and answers whether it was ``allowed`` and ``sent``."""
    op, _, rest = raw.partition(" ")
    try:
        data = json.loads(rest.strip())
    except ValueError:
        return reply(ok=False, error="not JSON")
    if not isinstance(data, dict) or not isinstance(data.get("device_id"), str):
        return reply(ok=False, error="bad device_id")
    device = store.get(data["device_id"])
    if device is None:
        return reply(ok=False, error="not registered")
    if op == "get":
        return reply(ok=True, profiles=store.allowed_profiles(device.device_id))
    if op == "set":
        try:
            return reply(ok=True, profiles=store.set_allowed_profiles(device.device_id, data.get("profiles")))
        except ValueError as exc:
            return reply(ok=False, error=str(exc))
    if op == "clear":
        store.clear_allowed_profiles(device.device_id)
        return reply(ok=True, profiles=None)
    if op == "test":
        profile = data.get("profile")
        if not isinstance(profile, str) or not PROFILE_RE.match(profile):
            return reply(ok=False, error="bad profile")
        if not store.allowlist().allows(device.device_id, profile):
            return reply(ok=True, allowed=False, sent=False)
        _, _, sig = store.identity()
        payload = push.message("bot_message", bot=profile, label=profile, session="herald-profile-test",
                               text=f"Synthetic test message for the {profile} profile. Nothing to do.")
        return reply(ok=True, allowed=True, sent=push.publish(device, payload, sig))
    return reply(ok=False, error=USAGE)


def make_handler(store: Store, version: str, watching: Callable[[], bool],
                 trusted: Callable[[], bool] = trusted_surface,
                 phones: Optional[Phones] = None, root: Optional[Path] = None) -> Callable[[str], str]:
    def reply(**data) -> str:
        return json.dumps(data, separators=(",", ":"))

    def handle(raw: str) -> str:
        # Exceptions would be swallowed into a generic "unknown command": always answer.
        try:
            if not trusted():
                return reply(ok=False, error="Herald push can only be managed from the Herald app or the dashboard.")
            raw = (raw or "").strip()
            if len(raw) > (MAX_ANSWER_ARG if raw.startswith("phone answer ") else MAX_ARG):
                return reply(ok=False, error="too long")
            op, _, rest = raw.partition(" ")
            rest = rest.strip()
            if op in ("", "info"):
                return reply(ok=True, version=version, gateway=store.public_identity(), watching=watching(),
                             devices=len(store.devices()), phone_tools=sorted(TOOLS), features=FEATURES)
            if op == "phone" and phones is not None:
                return phone_command(phones, rest, reply)
            if op == "cron":
                return cron_command(store, rest, reply, root)
            if op == "profiles":
                return profiles_command(store, rest, reply)
            if op == "register":
                try:
                    data = json.loads(rest)
                    device = Device.parse(data)
                    # The phone's allowed profiles travel with the registration, so a renewed phone is limited
                    # from its first push. Without them (an older app), a list it already has stays as it is.
                    profiles = data.get("profiles") if isinstance(data, dict) else None
                    store.upsert(device, profiles)
                except ValueError as exc:  # InvalidRegistration and JSON errors
                    return reply(ok=False, error=str(exc) if isinstance(exc, InvalidRegistration) else "not JSON")
                log.info("herald-push: registered %s", device.name)
                return reply(ok=True, version=version, gateway=store.public_identity(), watching=watching(),
                             features=FEATURES, profiles=store.allowed_profiles(device.device_id))
            if op == "unregister":
                return reply(ok=store.remove(rest))
            if op == "test":
                device = store.get(rest)
                if device is None:
                    return reply(ok=False, error="not registered")
                _, _, sig = store.identity()
                return reply(ok=push.publish(device, push.message("ping"), sig))
            return reply(ok=False, error=USAGE)
        except Exception as exc:
            log.exception("herald-push: command failed")
            return reply(ok=False, error=type(exc).__name__)

    return handle
