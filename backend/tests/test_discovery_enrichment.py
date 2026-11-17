from app.models.prospecting import Company, ICPProfile
from app.services.company_normalization import normalize_company_name, normalize_website
from app.services.qualification import qualify_against_icp


def test_company_normalization_is_stable() -> None:
    assert normalize_company_name("Clínica São José Odontologia") == "clinica sao jose odontologia"
    url, domain = normalize_website("www.Example.com/path?utm_source=test")
    assert url == "https://www.example.com/path"
    assert domain == "example.com"


def test_invalid_website_is_not_normalized() -> None:
    assert normalize_website("javascript:alert(1)") == (None, None)


def test_icp_qualification_explains_fit_and_confidence() -> None:
    company = Company(
        name="Clínica Exemplo", industry="Odontologia", website_url="https://example.com",
        normalized_domain="example.com", address={"city": "São Paulo"}, employee_count=12,
        source_url="https://example.com",
    )
    icp = ICPProfile(
        name="Odonto SP", industry="Odontologia", geography={"city": "São Paulo"},
        company_size={"min_employees": 5, "max_employees": 30}, exclusions=[],
    )
    result = qualify_against_icp(company, icp)
    assert result["fit_score"] == 100
    assert result["opportunity_score"] >= result["fit_score"] * 0.75
    assert 0.35 <= result["confidence"] <= 0.95
    assert len(result["checks"]) == 4
    assert "não é probabilidade" in result["interpretation"]


def test_icp_exclusions_reduce_fit_score() -> None:
    company = Company(name="Clínica Exemplo", industry="Odontologia", description="Atendimento popular")
    icp = ICPProfile(name="Premium", industry="Odontologia", exclusions=["popular"])
    result = qualify_against_icp(company, icp)
    assert result["fit_score"] == 85
    assert any(item["criterion"] == "exclusions" and not item["matched"] for item in result["checks"])


def test_decision_maker_name_normalization_and_source_policy() -> None:
    from app.services.decision_maker_intelligence import normalize_person_name, validate_public_source_url

    assert normalize_person_name(" Dra. Ana Júlia-Silva ") == "dra ana julia silva"
    assert validate_public_source_url("https://www.gov.br/receitafederal") == "https://www.gov.br/receitafederal"
    import pytest
    with pytest.raises(ValueError):
        validate_public_source_url("https://127.0.0.1/private")
    with pytest.raises(ValueError):
        validate_public_source_url("https://www.linkedin.com/in/example")


def test_decision_maker_confidence_requires_role_and_affiliation_evidence() -> None:
    from datetime import UTC, datetime
    from app.models.prospecting import DecisionMakerEvidence
    from app.services.decision_maker_intelligence import calculate_decision_maker_confidence

    now = datetime.now(UTC)
    role = DecisionMakerEvidence(
        claim_type="role", source_type="official_registry", source_name="Receita Federal",
        source_url="https://www.gov.br/receitafederal", authority_weight=100,
        observed_at=now, source_published_at=now, is_current=True,
    )
    incomplete = calculate_decision_maker_confidence([role], now)
    assert incomplete["status"] == "unconfirmed"
    assert "Falta comprovar cargo e vínculo com a empresa." in incomplete["explanation"]

    affiliation = DecisionMakerEvidence(
        claim_type="company_affiliation", source_type="company_website", source_name="Site institucional",
        source_url="https://example.com/equipe", authority_weight=90,
        observed_at=now, source_published_at=now, is_current=True,
    )
    complete = calculate_decision_maker_confidence([role, affiliation], now)
    assert complete["status"] == "current"
    assert complete["score"] >= 85
    assert complete["level"] == "high"


def test_decision_maker_confidence_penalizes_stale_evidence() -> None:
    from datetime import UTC, datetime, timedelta
    from app.models.prospecting import DecisionMakerEvidence
    from app.services.decision_maker_intelligence import calculate_decision_maker_confidence

    now = datetime.now(UTC)
    fresh = [
        DecisionMakerEvidence(claim_type="role", source_name="Registry", authority_weight=100, observed_at=now, source_published_at=now, is_current=True),
        DecisionMakerEvidence(claim_type="company_affiliation", source_name="Website", authority_weight=90, observed_at=now, source_published_at=now, is_current=True),
    ]
    stale = [
        DecisionMakerEvidence(claim_type="role", source_name="Registry", authority_weight=100, observed_at=now, source_published_at=now-timedelta(days=900), is_current=True),
        DecisionMakerEvidence(claim_type="company_affiliation", source_name="Website", authority_weight=90, observed_at=now, source_published_at=now-timedelta(days=900), is_current=True),
    ]
    assert calculate_decision_maker_confidence(fresh, now)["score"] > calculate_decision_maker_confidence(stale, now)["score"]


def test_missing_publication_date_does_not_receive_maximum_freshness() -> None:
    from datetime import UTC, datetime
    from app.services.decision_maker_intelligence import freshness_factor

    assert freshness_factor(None, datetime.now(UTC)) == 0.70


def test_decision_maker_job_title_classification() -> None:
    from app.services.decision_maker_intelligence import classify_job_title

    assert classify_job_title("Sócio administrador") == "partner"
    assert classify_job_title("Diretora clínica") == "director"
    assert classify_job_title("Gerente de marketing") == "manager"
    assert classify_job_title("Fundador") == "owner"
