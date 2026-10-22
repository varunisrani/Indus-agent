"""Two profiles under one root for the wave-42 multiplex demos: the launch profile (``default``,
at the root) with bot A, profile ``b`` with bot B, a fake Telegram platform per bot token, and the
host daemon over them (``gateway.multiplex_profiles: true``)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from indus.foundation.clock import SystemClock
from indus.foundation.scope import EnvView, Scope, make_scope
from indus.foundation.secrets import open_secret_store
from indus.gateway.adapters.base import Adapter
from indus.gateway.core.events import Source
from indus.gateway.core.session_keys import build_session_key
from indus.gateway.daemon.parts import DaemonPart
from indus.gateway.daemon.process import (
    AdapterConfig,
    GatewayDaemon,
    PartsFactory,
    SurfaceFactory,
)
from indus.gateway.daemon.side_services import ControlPart
from indus.gateway.registry import PlatformEntry, PlatformRegistry, policy_env_name
from indus.state import gateway_routes as routes
from tests.fakes.clock import FakeClock
from tests.fakes.model import FakeModelServer
from tests.fakes.platform.core import FakeChatPlatform
from tests.integration.gateway.daemon_kit import fake_service, quick
from tests.unit.gateway.core._kit import FakeChatAdapter

__all__ = [
    "ALLOW",
    "API_ON",
    "B_CONFIG",
    "MULTIPLEX",
    "ONLY_A",
    "ONLY_B",
    "TOKEN",
    "World",
    "dm_key",
    "session_of",
    "token",
]

TOKEN = "TELEGRAM_BOT_TOKEN"
ALLOW = "INDUS_TELEGRAM_ALLOWED_USERS"
ONLY_A = "100"  # allowed by A only
ONLY_B = "200"  # allowed by B only
MULTIPLEX = "gateway:\n  multiplex_profiles: true\n"
API_ON = "  api_server: {enabled: true, port: 0}\n"
B_CONFIG = "security:\n  secret_store: file\n"


def token(name: str) -> str:
    """A fake bot credential, built at runtime (never a credential-shaped literal)."""
    return "-".join(("fake", name, "bot", "credential"))


class World:
    """Two bots, the root, the launch scope and B's home."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, config: str) -> None:
        self.root = tmp_path / "r"
        self.root.mkdir()
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        monkeypatch.setenv(TOKEN, token("a"))  # the launch process env: A only
        monkeypatch.setenv(ALLOW, ONLY_A)
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        monkeypatch.delenv("INDUS_GATEWAY_SUPERVISED", raising=False)
        (self.root / "config.yaml").write_text(MULTIPLEX + config, encoding="utf-8")
        self.home_b = self.root / "profiles" / "b"
        self.home_b.mkdir(parents=True)
        (self.home_b / "profile.yaml").touch()
        (self.home_b / "config.yaml").write_text(B_CONFIG + MULTIPLEX, encoding="utf-8")
        store_b = open_secret_store(self.home_b, "b", mode="file")
        store_b.set(TOKEN, token("b"))
        store_b.set(ALLOW, ONLY_B)
        self.lock_dir = tmp_path / "token-locks"
        now = FakeClock(datetime.now(UTC))
        self.bots = {
            token("a"): FakeChatPlatform("telegram", clock=now),
            token("b"): FakeChatPlatform("telegram", clock=now),
        }
        self.bot_a = self.bots[token("a")]
        self.bot_b = self.bots[token("b")]
        self.built: list[str] = []

    def scope(self, home: Path | None = None) -> Scope:
        where = home or self.root
        env = EnvView.from_process().with_overlay(
            {"INDUS_HOME": str(where), "INDUS_GATEWAY_LOCK_DIR": str(self.lock_dir)}
        )
        scope = make_scope(where, env=env, clock=SystemClock())
        mode_profile = "default" if home is None else home.name
        return scope.with_secrets(open_secret_store(where, mode_profile, mode="file"))

    def registry(self) -> PlatformRegistry:
        def factory(config: AdapterConfig) -> Adapter:
            # The bot is chosen by the token the profile's own settings carry.
            credential = config.settings.credentials.get(TOKEN, "")
            self.built.append(f"{config.scope.profile_id}:{credential == token('b')}")
            return FakeChatAdapter(
                self.bots[credential], clock=config.clock, settings=config.settings
            )

        entry = PlatformEntry(
            name="telegram",
            label="Telegram",
            factory=factory,
            check_fn=lambda: True,
            required_env=(TOKEN,),
            install_hint="",
            allowlist_env=policy_env_name("telegram", "ALLOWED_USERS"),
            allow_all_env=policy_env_name("telegram", "ALLOW_ALL_USERS"),
            allow_bots_env=policy_env_name("telegram", "ALLOW_BOTS"),
            home_env=policy_env_name("telegram", "HOME_CHANNEL"),
            max_len=4096,
        )
        return PlatformRegistry(builtins=(entry,))

    def host(
        self,
        server: FakeModelServer,
        tmp_path: Path,
        *,
        parts: Sequence[DaemonPart] | None = None,
        secondary_parts: PartsFactory | None = None,
        surfaces: SurfaceFactory | None = None,
    ) -> GatewayDaemon:
        """The host daemon (a control socket by default; served profiles get no parts unless
        ``secondary_parts`` says otherwise)."""
        own: Sequence[DaemonPart] = parts if parts is not None else [ControlPart()]
        return GatewayDaemon(
            self.scope(),
            options=quick(),
            registry=self.registry(),
            service_factory=fake_service(server, tmp_path / "svc-locks"),
            parts=own,
            secondary_parts=secondary_parts or _no_parts,
            surface_factory=surfaces,
        )


def _no_parts(_daemon: GatewayDaemon) -> list[DaemonPart]:
    return []


def dm_key(profile: str, user: str) -> str:
    source = Source(platform="telegram", chat_id=user, chat_type="dm", user_id=user)
    return build_session_key(source, profile=profile)


def session_of(daemon: GatewayDaemon, key: str) -> str | None:
    entry = routes.get(daemon.store, key)
    return None if entry is None else entry.session_id
