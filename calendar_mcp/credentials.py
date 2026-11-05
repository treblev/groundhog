"""Encrypted credential file with its master key held only in an OS keyring."""

import json
import os
import stat
import tempfile

import keyring
from cryptography.fernet import Fernet

from integrations.icloud_calendar import CalendarError


def _os_keyring():
    backend = keyring.get_keyring()
    candidates = getattr(backend, "backends", [backend])
    for candidate in candidates:
        if type(candidate).__module__ in {
            "keyring.backends.macOS", "keyring.backends.SecretService", "keyring.backends.Windows",
            "keyring.backends.kwallet",
        }:
            return candidate
    raise CalendarError("An unlocked OS keyring is required. Plaintext and null keyring backends are not supported.")


class SecretStore:
    def __init__(self, settings, backend=None):
        self.settings = settings
        self.backend = backend if backend is not None else _os_keyring()

    def _key(self, *, create=False):
        key = self.backend.get_password(self.settings.keyring_service, self.settings.keyring_account)
        if key is None and create:
            key = Fernet.generate_key().decode()
            self.backend.set_password(self.settings.keyring_service, self.settings.keyring_account, key)
        if key is None:
            raise CalendarError("Calendar credentials are not connected or the OS keyring is locked.")
        return key.encode()

    def set(self, username: str, password: str):
        if not username.strip() or not password.strip():
            raise CalendarError("Apple Account email and app-specific password are required.")
        encrypted = Fernet(self._key(create=True)).encrypt(json.dumps(
            {"username": username.strip(), "password": password.strip()}).encode())
        path = self.settings.credential_file
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def get(self):
        try:
            with self.settings.credential_file.open("rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                    raise ValueError
                credentials = json.loads(Fernet(self._key()).decrypt(stream.read()))
            if not isinstance(credentials, dict) or any(
                not isinstance(credentials.get(k), str) or not credentials[k].strip()
                for k in ("username", "password")
            ):
                raise ValueError
            return {k: credentials[k] for k in ("username", "password")}
        except Exception:
            raise CalendarError("Cannot unlock calendar credentials. Check the private encrypted file and the OS keyring.") from None

    def disconnect(self):
        self.settings.credential_file.unlink(missing_ok=True)
        if self.backend.get_password(self.settings.keyring_service, self.settings.keyring_account) is not None:
            self.backend.delete_password(self.settings.keyring_service, self.settings.keyring_account)
