"""The gateway's push identity and the phones registered with it, in one directory shared by every profile.

Files are written atomically with mode 0600: the gateway's private keys are in ``identity.json``, and the
phones' topics (which are what an outsider would need to even see the ciphertext) in ``devices.json``.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

from . import crypto

TOPIC_RE = re.compile(r"^hp-[0-9a-f]{32}$")
DEVICE_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
SERVER_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$")
MAX_DEVICES = 20
# A profile's name, as Hermes allows it (also the cron module's PROFILE_RE).
PROFILE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_PROFILES = 200
# What a phone's list falls back to when the file that holds the lists can't be read: fail closed, never open.
SAFE_PROFILES = ("default",)
MAX_NAME = 64
_REPLACE_TRIES = 6


class InvalidRegistration(ValueError):
    pass


@dataclass(frozen=True)
class Device:
    device_id: str
    name: str
    server: str
    push_topic: str
    reply_topic: str
    enc_pub: str
    sig_pub: str

    @staticmethod
    def parse(data: object) -> "Device":
        """A registration from a phone, refused unless every field is well-formed."""
        if not isinstance(data, dict):
            raise InvalidRegistration("registration must be an object")
        try:
            fields = {k: data[k] for k in ("device_id", "name", "server", "push_topic", "reply_topic", "enc_pub", "sig_pub")}
        except KeyError as exc:
            raise InvalidRegistration(f"missing {exc.args[0]}") from exc
        if not all(isinstance(v, str) for v in fields.values()):
            raise InvalidRegistration("fields must be strings")
        if not DEVICE_ID_RE.match(fields["device_id"]):
            raise InvalidRegistration("bad device_id")
        server = fields["server"].strip().rstrip("/")
        if not SERVER_RE.match(server):
            raise InvalidRegistration("server must be an https origin")
        if not TOPIC_RE.match(fields["push_topic"]) or not TOPIC_RE.match(fields["reply_topic"]):
            raise InvalidRegistration("bad topic")
        if fields["push_topic"] == fields["reply_topic"]:
            raise InvalidRegistration("topics must differ")
        try:
            crypto.decode_public(fields["enc_pub"])
            crypto.decode_public(fields["sig_pub"])
        except crypto.Rejected as exc:
            raise InvalidRegistration(f"bad key: {exc}") from exc
        name = "".join(ch for ch in fields["name"] if ch.isprintable())[:MAX_NAME].strip() or "Phone"
        return Device(**{**fields, "server": server, "name": name})


def _write_private(path: Path, data: object) -> None:
    """Atomic write, readable by this user only, from the first byte."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(_REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows refuses to replace a file another process has open for a moment (a reader).
                if sys.platform != "win32" or attempt == _REPLACE_TRIES - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# One lock for every Store in this process (the plugin is loaded once per profile, each with its own Store),
# plus a file lock for the other Hermes processes (dashboard, messaging gateway) sharing the same files.
_process_lock = threading.RLock()


@contextmanager
def _exclusive(root: Path):
    with _process_lock:
        root.mkdir(parents=True, exist_ok=True)
        with open(root / ".lock", "a+") as fh:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)
                while True:
                    try:
                        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                        break
                    except OSError:  # LK_LOCK gives up after ~10 s; keep waiting
                        continue
                try:
                    yield
                finally:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def clean_profiles(profiles: object) -> List[str]:
    """A phone's allowed profiles, checked: a list of well-formed names, without repeats. Anything else is refused,
    never trimmed to something that looks valid."""
    if not isinstance(profiles, list) or len(profiles) > MAX_PROFILES:
        raise ValueError("profiles must be a list of at most %d names" % MAX_PROFILES)
    seen: List[str] = []
    for name in profiles:
        if not isinstance(name, str) or not PROFILE_RE.match(name):
            raise ValueError("bad profile name")
        if name not in seen:
            seen.append(name)
    return sorted(seen)


class Allowlist:
    """One look at which profiles each phone may be sent. A phone with a list gets exactly its profiles: one that
    isn't on it, whether unknown or made after the list was, is not sent. A phone with no list is sent everything,
    as before profiles could be limited. If the lists can't be read, every phone gets [SAFE_PROFILES] only."""

    def __init__(self, lists: Dict[str, List[str]], unreadable: bool = False):
        self._lists = lists
        self.unreadable = unreadable

    def get(self, device_id: str) -> Optional[List[str]]:
        """The phone's list; None when it has none (or, unreadable, the safe one)."""
        if self.unreadable:
            return list(SAFE_PROFILES)
        return self._lists.get(device_id)

    def allows(self, device_id: str, profile: str) -> bool:
        allowed = self.get(device_id)
        return True if allowed is None else profile in allowed


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _lock(self):
        return _exclusive(self.root)

    # ── identity ────────────────────────────────────────────────────────────

    def identity(self):
        """(gateway_id, enc private key, sig private key), made once."""
        path = self.root / "identity.json"
        with self._lock():
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                return (data["gateway_id"], crypto.private_from_pem(data["enc"].encode()),
                        crypto.private_from_pem(data["sig"].encode()))
            gateway_id = str(uuid.uuid4())
            enc, sig = crypto.generate_key(), crypto.generate_key()
            _write_private(path, {"gateway_id": gateway_id, "enc": crypto.private_to_pem(enc).decode(),
                                  "sig": crypto.private_to_pem(sig).decode()})
            return gateway_id, enc, sig

    def public_identity(self) -> dict:
        gateway_id, enc, sig = self.identity()
        return {"gateway_id": gateway_id, "enc_pub": crypto.encode_public(enc.public_key()),
                "sig_pub": crypto.encode_public(sig.public_key()), "version": crypto.VERSION}

    # ── devices ─────────────────────────────────────────────────────────────

    def devices(self) -> List[Device]:
        path = self.root / "devices.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        out = []
        for entry in raw.get("devices", []) if isinstance(raw, dict) else []:
            try:
                out.append(Device.parse(entry))
            except InvalidRegistration:
                continue
        return out

    def upsert(self, device: Device, profiles: Optional[List[str]] = None) -> None:
        """Registers [device]. With [profiles] its list is stored too, before the phone is listed: a reader that
        sees the phone then sees its list, so a renewed phone is never sent a push the list would have stopped.
        Without them, a list the phone already has is left as it is."""
        try:
            cleaned = None if profiles is None else clean_profiles(profiles)
        except ValueError as exc:
            raise InvalidRegistration(str(exc)) from exc
        with self._lock():
            kept = [d for d in self.devices() if d.device_id != device.device_id]
            # A topic belongs to one phone: a second registration can't take over another phone's topics.
            taken = {t for d in kept for t in (d.push_topic, d.reply_topic)}
            if device.push_topic in taken or device.reply_topic in taken:
                raise InvalidRegistration("topic already registered by another phone")
            if len(kept) >= MAX_DEVICES:
                raise InvalidRegistration("too many phones registered")
            if cleaned is not None:
                self._write_profiles({d.device_id for d in kept + [device]}, device.device_id, cleaned)
            _write_private(self.root / "devices.json", {"devices": [asdict(d) for d in kept + [device]]})

    def remove(self, device_id: str) -> bool:
        with self._lock():
            devices = self.devices()
            kept = [d for d in devices if d.device_id != device_id]
            if len(kept) == len(devices):
                return False
            # The list goes first, then the phone: no list is left behind for an id that comes back.
            self._write_profiles({d.device_id for d in kept}, None, None)
            _write_private(self.root / "devices.json", {"devices": [asdict(d) for d in kept]})
            return True

    def get(self, device_id: str) -> Optional[Device]:
        return next((d for d in self.devices() if d.device_id == device_id), None)

    # ── allowed profiles ────────────────────────────────────────────────────

    @property
    def _profiles_path(self) -> Path:
        return self.root / "profiles.json"

    def allowlist(self) -> Allowlist:
        """The lists as they are now. A sender reads the devices first and this after, so a phone it sees has its list."""
        path = self._profiles_path
        if not path.exists():
            return Allowlist({})
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("not an object")
            return Allowlist({str(k): clean_profiles(v) for k, v in raw.items()})
        except (OSError, ValueError):
            return Allowlist({}, unreadable=True)

    def _write_profiles(self, registered: set, device_id: Optional[str], profiles: Optional[List[str]]) -> None:
        """Rewrites the lists (the caller holds the lock): those of phones still registered, [device_id]'s set to
        [profiles] when given. A file that can't be read is replaced only by a list being set."""
        current = self.allowlist()
        lists = {} if current.unreadable else {k: v for k, v in current._lists.items() if k in registered}
        if current.unreadable and profiles is None:
            return
        if device_id is not None and profiles is not None:
            lists[device_id] = profiles
        if lists or self._profiles_path.exists():
            _write_private(self._profiles_path, lists)

    def allowed_profiles(self, device_id: str) -> Optional[List[str]]:
        return self.allowlist().get(device_id)

    def set_allowed_profiles(self, device_id: str, profiles: object) -> List[str]:
        """Sets what a registered phone may be sent. ValueError for a bad list or an unknown phone."""
        cleaned = clean_profiles(profiles)
        with self._lock():
            registered = {d.device_id for d in self.devices()}
            if device_id not in registered:
                raise ValueError("not registered")
            self._write_profiles(registered, device_id, cleaned)
        return cleaned

    def clear_allowed_profiles(self, device_id: str) -> None:
        """Back to no list: the phone is sent every profile, as before lists existed."""
        with self._lock():
            current = self.allowlist()
            if current.unreadable:
                return
            lists = {k: v for k, v in current._lists.items() if k != device_id}
            _write_private(self._profiles_path, lists)

    # ── watcher state ───────────────────────────────────────────────────────

    def marks(self) -> Dict[str, int]:
        try:
            raw = json.loads((self.root / "marks.json").read_text(encoding="utf-8"))
            return {str(k): int(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def save_marks(self, marks: Dict[str, int]) -> None:
        _write_private(self.root / "marks.json", marks)

    def cron_marks(self) -> Dict[str, str]:
        """Per job's output folder (``cron:<profile>:<job>``), the newest run the watcher has seen."""
        try:
            raw = json.loads((self.root / "cron-marks.json").read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def save_cron_marks(self, marks: Dict[str, str]) -> None:
        _write_private(self.root / "cron-marks.json", marks)
