from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class DiscoveryRequest:
    query: str
    filters: dict[str, Any]
    icp: dict[str, Any]


@dataclass(frozen=True)
class DiscoveredCompany:
    name: str
    website_url: str | None = None
    phone: str | None = None
    email: str | None = None
    address: dict[str, Any] | None = None
    industry: str | None = None
    source_url: str | None = None
    external_id: str | None = None
    raw: dict[str, Any] | None = None


class DiscoveryProvider(Protocol):
    async def discover(self, request: DiscoveryRequest) -> list[DiscoveredCompany]: ...


class ManualDiscoveryProvider:
    async def discover(self, request: DiscoveryRequest) -> list[DiscoveredCompany]:
        # A queued run is persisted now; manual records are added through the companies endpoint.
        # This provider deliberately does not fabricate or synthesize prospects.
        return []


class ApifyDiscoveryProvider:
    """Adapter contract. Actor execution is enabled only after credentials and actor mapping are configured."""

    def __init__(self, token: str | None, actor_id: str | None) -> None:
        self.token = token
        self.actor_id = actor_id

    async def discover(self, request: DiscoveryRequest) -> list[DiscoveredCompany]:
        if not self.token or not self.actor_id:
            raise RuntimeError("Apify integration is not configured for this environment")
        raise NotImplementedError("Apify actor execution is scheduled for the next integration milestone")
