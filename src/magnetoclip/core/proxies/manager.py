"""Proxy profile management."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import sessionmaker

from ...app.context import AppContext
from ...database.models import ProxyProfile
from ...database.repositories import ProxyRepository
from ...network.proxy.profiles import ProxySpec
from ...services.logging import get_logger
from ..events.bus import Events

log = get_logger(__name__)


class ProxyManager:
    """CRUD over the ``proxy_profiles`` table plus spec conversion."""

    def __init__(self, context: AppContext) -> None:
        self.context = context
        self.session_factory: sessionmaker = context.session_factory
        self._profiles: dict[int, ProxyProfile] = {}

    def reload(self) -> None:
        with self.session_factory() as session:
            profiles = ProxyRepository(session).list()
        self._profiles = {profile.id: profile for profile in profiles}
        self.context.events.post(
            Events.PROXIES_CHANGED, {"profiles": [p.name for p in profiles]}
        )

    def list(self) -> list[ProxyProfile]:
        return list(self._profiles.values())

    def get(self, profile_id: int) -> ProxyProfile | None:
        return self._profiles.get(profile_id)

    def get_by_name(self, name: str) -> ProxyProfile | None:
        for profile in self._profiles.values():
            if profile.name == name:
                return profile
        return None

    def add(
        self,
        name: str,
        *,
        proxy_type: str = "direct",
        host: str | None = None,
        port: int | None = None,
        username_ref: str | None = None,
    ) -> ProxyProfile:
        with self.session_factory() as session:
            repo = ProxyRepository(session)
            if repo.get_by_name(name) is not None:
                raise ValueError(f"proxy profile '{name}' already exists")
            profile = repo.add(
                name,
                proxy_type=proxy_type,
                host=host,
                port=port,
                username_ref=username_ref,
            )
        self.reload()
        return profile

    def remove(self, profile_id: int) -> None:
        with self.session_factory() as session:
            repo = ProxyRepository(session)
            profile = repo.get(profile_id)
            if profile is None:
                raise KeyError(profile_id)
            repo.remove(profile)
        self.reload()

    def to_spec(self, profile_id: int | None) -> ProxySpec | None:
        profile = self.get(profile_id) if profile_id is not None else None
        if profile is None:
            return None
        return ProxySpec(
            type=profile.type,
            host=profile.host,
            port=profile.port,
            username=profile.username_ref,
        )

    def to_dict(self, profile: ProxyProfile) -> dict[str, Any]:
        return {
            "id": profile.id,
            "name": profile.name,
            "type": profile.type,
            "host": profile.host,
            "port": profile.port,
            "username": profile.username_ref,
        }
