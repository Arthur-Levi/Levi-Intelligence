from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from typing import Iterable
from urllib.parse import urlsplit

from app.models.prospecting import DecisionMakerEvidence

# Source classes are deliberately allow-listed. LinkedIn scraping and access-control
# circumvention are not supported; use only approved APIs or licensed providers.
SOURCE_AUTHORITY = {
    "official_registry": 100,
    "company_website": 90,
    "licensed_provider": 85,
    "professional_publication": 75,
    "public_directory": 60,
    "manual_attestation": 40,
}
ALLOWED_CLAIMS = {"role", "company_affiliation", "ownership"}


def normalize_person_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.strip().casefold())
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", normalized)).strip()


def validate_public_source_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("A fonte deve ser uma URL pública HTTP(S) válida")
    host = parsed.hostname.casefold()
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith((".local", ".internal")):
        raise ValueError("Fontes internas ou locais não são permitidas")
    if host == "linkedin.com" or host.endswith(".linkedin.com") or host == "lnkd.in":
        raise ValueError("LinkedIn não é uma fonte automatizada autorizada neste conector")
    return value.strip()


def freshness_factor(observed_at: datetime | None, now: datetime | None = None) -> float:
    # If the source does not publish a date, do not assume that a fresh fetch
    # proves the underlying claim itself is current.
    if observed_at is None:
        return 0.70
    now = now or datetime.now(UTC)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    age_days = max(0, (now - observed_at).days)
    if age_days <= 90:
        return 1.0
    if age_days <= 365:
        return 0.85
    if age_days <= 730:
        return 0.65
    return 0.35


def calculate_decision_maker_confidence(
    evidence: Iterable[DecisionMakerEvidence], now: datetime | None = None
) -> dict[str, object]:
    current = [item for item in evidence if item.is_current]
    if not current:
        return {
            "score": 0,
            "level": "insufficient",
            "status": "unconfirmed",
            "claim_types": [],
            "source_count": 0,
            "explanation": ["Nenhuma evidência profissional atual foi registrada."],
        }

    weighted = []
    sources = set()
    claim_types = set()
    for item in current:
        base = min(100, max(0, int(item.authority_weight)))
        factor = freshness_factor(item.source_published_at, now)
        weighted.append(base * factor)
        sources.add(item.source_name.casefold())
        claim_types.add(item.claim_type)

    base_score = sum(weighted) / len(weighted)
    corroboration = min(10, max(0, (len(sources) - 1) * 5))
    score = min(98, round(base_score + corroboration))
    enough_claims = "role" in claim_types and "company_affiliation" in claim_types
    status = "current" if score >= 70 and enough_claims else "unconfirmed"
    if score >= 85 and enough_claims:
        level = "high"
    elif score >= 65:
        level = "medium"
    else:
        level = "low"
    explanation = [f"Autoridade média das fontes ajustada pela atualidade: {round(base_score)} pontos."]
    explanation.append(f"Corroboração por {len(sources)} fonte(s) distinta(s): +{corroboration} pontos.")
    if not enough_claims:
        explanation.append("Falta comprovar cargo e vínculo com a empresa.")
    return {
        "score": score,
        "level": level,
        "status": status,
        "claim_types": sorted(claim_types),
        "source_count": len(sources),
        "explanation": explanation,
    }


def authority_for_source(source_type: str) -> int:
    try:
        return SOURCE_AUTHORITY[source_type]
    except KeyError:
        raise ValueError("Tipo de fonte não autorizado") from None


def classify_job_title(job_title: str | None) -> str:
    if not job_title:
        return "other"
    title = normalize_person_name(job_title)
    if any(token in title for token in ("socio administrador", "partner", "socio", "socia")):
        return "partner"
    if any(token in title for token in ("proprietario", "proprietaria", "fundador", "fundadora", "owner")):
        return "owner"
    if any(token in title for token in ("diretor", "diretora", "director", "ceo", "presidente")):
        return "director"
    if any(token in title for token in ("gerente", "gestor", "gestora", "manager", "coordenador", "coordenadora")):
        return "manager"
    if any(token in title for token in ("compras", "procurement", "suprimentos")):
        return "procurement"
    if any(token in title for token in ("marketing", "comunicacao")):
        return "marketing"
    return "other"
