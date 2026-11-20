from typing import Any

from app.models.prospecting import Company, ICPProfile


def qualify_company(company: Company) -> tuple[int, list[str]]:
    """Transparent completeness score based only on observed company fields."""
    score = 0
    reasons: list[str] = []
    if company.website_url:
        score += 20
        reasons.append("Possui website informado")
    if company.phone:
        score += 15
        reasons.append("Possui telefone informado")
    if company.email:
        score += 15
        reasons.append("Possui e-mail informado")
    if company.address:
        score += 15
        reasons.append("Possui endereço informado")
    if company.industry:
        score += 15
        reasons.append("Possui segmento informado")
    if company.employee_count is not None:
        score += 10
        reasons.append("Possui estimativa de porte informada")
    if company.source_url:
        score += 10
        reasons.append("Possui URL de origem para verificação")
    return min(score, 100), reasons


def qualify_against_icp(company: Company, icp: ICPProfile) -> dict[str, Any]:
    """Rule-based fit score; never represents a purchase probability."""
    checks: list[dict[str, Any]] = []
    industry_match = bool(
        company.industry and company.industry.casefold() == icp.industry.casefold()
    )
    checks.append(
        {
            "criterion": "industry",
            "matched": industry_match,
            "weight": 40,
            "reason": "Segmento compatível"
            if industry_match
            else "Segmento não confirmado como compatível",
        }
    )

    geography = icp.geography or {}
    city = (company.address or {}).get("city")
    target_city = geography.get("city")
    geo_match = not target_city or (city and str(city).casefold() == str(target_city).casefold())
    checks.append(
        {
            "criterion": "geography",
            "matched": bool(geo_match),
            "weight": 25,
            "reason": "Localização compatível"
            if geo_match
            else "Localização fora do alvo ou não confirmada",
        }
    )

    size = icp.company_size or {}
    count = company.employee_count
    min_count, max_count = size.get("min_employees"), size.get("max_employees")
    size_match = (
        count is not None
        and (min_count is None or count >= min_count)
        and (max_count is None or count <= max_count)
    )
    if min_count is None and max_count is None:
        size_match = True
    checks.append(
        {
            "criterion": "company_size",
            "matched": bool(size_match),
            "weight": 20,
            "reason": "Porte compatível" if size_match else "Porte não confirmado como compatível",
        }
    )

    exclusions = [str(item).casefold() for item in (icp.exclusions or [])]
    text = f"{company.name} {company.description or ''} {company.industry or ''}".casefold()
    excluded = any(item and item in text for item in exclusions)
    checks.append(
        {
            "criterion": "exclusions",
            "matched": not excluded,
            "weight": 15,
            "reason": "Nenhuma exclusão identificada"
            if not excluded
            else "Correspondência com critério de exclusão",
        }
    )

    score = sum(item["weight"] for item in checks if item["matched"])
    completeness, completeness_reasons = qualify_company(company)
    confidence = round(min(0.95, 0.35 + (completeness / 100) * 0.6), 2)
    opportunity = max(0, min(100, round(score * 0.75 + completeness * 0.25)))
    return {
        "fit_score": score,
        "completeness_score": completeness,
        "opportunity_score": opportunity,
        "confidence": confidence,
        "checks": checks,
        "completeness_reasons": completeness_reasons,
        "interpretation": (
            "Índice heurístico de aderência e completude; não é probabilidade de conversão."
        ),
    }
