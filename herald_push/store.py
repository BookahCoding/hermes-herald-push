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

    def upsert(self, device: Device) -> None:
        with self._lock():
            kept = [d for d in self.devices() if d.device_id != device.device_id]
            # A topic belongs to one phone: a second registration can't take over another phone's topics.
            taken = {t for d in kept for t in (d.push_topic, d.reply_topic)}
            if device.push_topic in taken or device.reply_topic in taken:
                raise InvalidRegistration("topic already registered by another phone")
            if len(kept) >= MAX_DEVICES:
                raise InvalidRegistration("too many phones registered")
            _write_private(self.root / "devices.json", {"devices": [asdict(d) for d in kept + [device]]})

    def remove(self, device_id: str) -> bool:
        with self._lock():
            devices = self.devices()
            kept = [d for d in devices if d.device_id != device_id]
            if len(kept) == len(devices):
                return False
            _write_private(self.root / "devices.json", {"devices": [asdict(d) for d in kept]})
            return True

    def get(self, device_id: str) -> Optional[Device]:
        return next((d for d in self.devices() if d.device_id == device_id), None)

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
