from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import OuraError


def private_dir(path):
    path = Path(path)
    if path.is_symlink():
        raise OuraError("unsafe_path", "State directories must not be symbolic links")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {} if default is None else default
    except (ValueError, UnicodeError):
        raise OuraError("invalid_state", "Local JSON state is invalid; restore its backup") from None


def write_json(path, value):
    path = Path(path)
    private_dir(path.parent)
    if path.is_symlink():
        raise OuraError("unsafe_path", "State files must not be symbolic links")
    fd, temp = tempfile.mkstemp(prefix=".atomic-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def validate_owner(name):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", name or ""):
        raise OuraError(
            "invalid_owner", "Owner must be 1–48 lowercase letters, digits, _ or -, starting with a letter"
        )
    return name


class Profile:
    def __init__(self, home, owner):
        self.home = Path(home).expanduser().absolute()
        self.owner = validate_owner(owner)
        self.path = self.home / "owners" / owner
        if self.path.is_symlink() or self.home.is_symlink():
            raise OuraError("unsafe_path", "Owner directory must not be a symbolic link")
        if not (self.path / "profile.json").is_file():
            raise OuraError("owner_not_found", "Create this owner with: oura profile create NAME")
        self.config = read_json(self.path / "profile.json")

    @property
    def timezone(self):
        return ZoneInfo(self.config.get("timezone", "UTC"))

    @property
    def secrets(self):
        return read_json(self.path / "secrets.json")

    def save_secrets(self, values):
        write_json(self.path / "secrets.json", values)

    def save_config(self):
        write_json(self.path / "profile.json", self.config)

    @contextlib.contextmanager
    def lock(self, name="owner"):
        private_dir(self.path)
        p = self.path / (name + ".lock")
        fd = os.open(p, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def owners(home):
    path = Path(home) / "owners"
    return sorted(p.parent.name for p in path.glob("*/profile.json") if not p.parent.is_symlink())


def create_profile(home, owner, timezone="UTC"):
    validate_owner(owner)
    try:
        ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError):
        raise OuraError("invalid_timezone", "Use an IANA timezone, for example Asia/Dubai") from None
    home = private_dir(home)
    private_dir(home / "owners")
    path = home / "owners" / owner
    if path.exists():
        raise OuraError("owner_exists", "Owner already exists; it was not overwritten")
    private_dir(path)
    write_json(path / "profile.json", {"schema_version": 1, "owner": owner, "timezone": timezone})
    return Profile(home, owner)


def select_profile(home, owner=None):
    if owner:
        return Profile(home, owner)
    found = owners(home)
    if len(found) != 1:
        raise OuraError("owner_required", "Select --owner NAME; create a profile first if none exists")
    return Profile(home, found[0])
