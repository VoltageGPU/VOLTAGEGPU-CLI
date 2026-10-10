"""Per-invocation state: builds the API client lazily so `--help` and offline
commands never need a key."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import click

from ..sdk.client import VoltageGPUClient
from ..sdk.config import Config


@dataclass
class AppState:
    base_url: Optional[str] = None
    _client: Optional[VoltageGPUClient] = field(default=None, repr=False)

    def client(self) -> VoltageGPUClient:
        if self._client is None:
            self._client = VoltageGPUClient(Config.load(base_url=self.base_url))
        return self._client

    def client_with_key(self, api_key: str) -> VoltageGPUClient:
        """A one-off client for a key that is not stored yet (used by `login`)."""
        return VoltageGPUClient(Config.load(api_key=api_key, base_url=self.base_url))

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


pass_state = click.make_pass_decorator(AppState, ensure=True)
