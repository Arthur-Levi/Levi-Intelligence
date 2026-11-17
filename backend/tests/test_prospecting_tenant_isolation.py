from uuid import uuid4

from sqlalchemy import select

from app.models.prospecting import Company, ICPProfile, ProspectingRun, SourceRecord
from app.services.qualification import qualify_company


def _sql(statement) -> str:
    return str(statement.compile(compile_kwargs={"literal_binds": True}))


def test_company_listing_query_is_tenant_scoped() -> None:
    tenant_id = uuid4()
    statement = select(Company).where(Company.tenant_id == tenant_id)
    sql = _sql(statement)
    assert "companies.tenant_id" in sql
    assert tenant_id.hex in sql.replace("-", "")


def test_icp_and_run_queries_have_tenant_scope() -> None:
    tenant_id = uuid4()
    icp_sql = _sql(select(ICPProfile).where(ICPProfile.tenant_id == tenant_id))
    run_sql = _sql(select(ProspectingRun).where(ProspectingRun.tenant_id == tenant_id))
    assert "icp_profiles.tenant_id" in icp_sql
    assert "prospecting_runs.tenant_id" in run_sql


def test_source_record_query_is_scoped_by_tenant_and_company() -> None:
    tenant_id, company_id = uuid4(), uuid4()
    sql = _sql(
        select(SourceRecord).where(
            SourceRecord.tenant_id == tenant_id,
            SourceRecord.company_id == company_id,
        )
    )
    assert "source_records.tenant_id" in sql
    assert "source_records.company_id" in sql


def test_qualification_is_transparent_and_bounded() -> None:
    company = Company(
        name="Clínica Exemplo",
        website_url="https://example.com",
        phone="+55 11 0000-0000",
        email="contato@example.com",
        address={"city": "São Paulo"},
        industry="Odontologia",
        employee_count=12,
        source_url="https://example.com/about",
    )
    score, reasons = qualify_company(company)
    assert score == 100
    assert len(reasons) == 7
    company.website_url = None
    score_without_site, reasons_without_site = qualify_company(company)
    assert score_without_site == 80
    assert all("website" not in reason.lower() for reason in reasons_without_site)
