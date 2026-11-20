"""Contracts for authorized professional-information providers.

Providers must use documented APIs, licensed datasets, official public registries,
or public company pages where automated access is permitted. This module intentionally
contains no LinkedIn crawler, credentialed-profile scraper, or access-control bypass.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class DecisionMakerCandidate:
    full_name: str
    job_title: str | None
    company_registration_id: str | None
    source_name: str
    source_url: str
    source_type: str
    claim_type: str
    source_published_at: datetime | None = None
    excerpt: str | None = None


class ProfessionalDataProvider(Protocol):
    provider_id: str

    async def find_people(
        self, *, company_name: str, company_registration_id: str | None = None
    ) -> list[DecisionMakerCandidate]: ...


SUPPORTED_PROVIDER_CLASSES = frozenset(
    {
        "official_registry",
        "company_website",
        "licensed_provider",
        "professional_publication",
        "public_directory",
    }
)


def validate_provider_candidate(candidate: DecisionMakerCandidate) -> DecisionMakerCandidate:
    from app.services.decision_maker_intelligence import (
        authority_for_source,
        validate_public_source_url,
    )

    if candidate.source_type not in SUPPORTED_PROVIDER_CLASSES:
        raise ValueError("Provider class is not supported")
    authority_for_source(candidate.source_type)
    validate_public_source_url(candidate.source_url)
    if candidate.claim_type not in {"role", "company_affiliation", "ownership"}:
        raise ValueError("Unsupported evidence claim")
    if not candidate.full_name.strip() or not candidate.source_name.strip():
        raise ValueError("Candidate must include a name and source")
    return candidate
