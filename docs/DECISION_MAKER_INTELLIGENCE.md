# Build 005 — Decision Maker Intelligence

## Objective

Associate public, relevant professional roles with the correct tenant-owned company, preserving evidence, observation time, source provenance, confidence rationale, and a change history.

## Permitted source classes

- Official public business registries and public corporate filings (e.g. QSA/ownership data where lawfully available).
- The company's own public website and team/leadership pages, where automated access is permitted.
- Licensed professional/business data providers with contractual rights to supply the data.
- Public professional publications and directories whose terms permit the intended use.
- Human-entered evidence with source URL and a recorded review.

LinkedIn scraping, profile crawling, automated account access, bypassing access controls, credential reuse, and unauthorized third-party data brokers are not supported. LinkedIn's User Agreement explicitly prohibits scraping/copying its services and unauthorized automation: https://www.linkedin.com/legal/user-agreement

## Data minimization and LGPD

The platform stores only business-relevant role and affiliation details required for B2B qualification. It must not ingest sensitive personal data, private contact details, credentials, personal addresses, or unrelated personal attributes. Each tenant remains responsible for documenting its purpose and legal basis, providing transparency and honoring data-subject requests. Legitimate interest is not a blanket authorization: apply purpose, necessity, balancing, safeguards, and a practical opt-out/rights workflow. See ANPD guidance: https://www.gov.br/anpd/pt-br/centrais-de-conteudo/materiais-educativos-e-publicacoes/guia_orientativo_hipoteses_legais_tratamento_de_dados_pessoais_legitimo_interesse

## Evidence model

Each evidence item supports one claim: `role`, `company_affiliation`, or `ownership`. Source classes receive fixed authority weights; clients cannot self-assign weights. Source publication date drives freshness. If a source has no publication date, the confidence engine applies a conservative freshness factor rather than treating the content as newly verified merely because it was fetched today.

A person is marked `current` only when the engine has current evidence for both role and company affiliation and a confidence score of at least 70. Confidence is an explainable evidence score, not a probability that the person will buy.

## API

- `GET /companies/{company_id}/decision-makers/intelligence`
- `POST /decision-makers/{person_id}/evidence`
- `GET /decision-makers/{person_id}/evidence`
- `POST /decision-makers/{person_id}/evidence/{evidence_id}/mark-stale`
- `PATCH /decision-makers/{person_id}/profile`
- `POST /decision-makers/{person_id}/reassess`
- `GET /decision-makers/{person_id}/history`

All endpoints require tenant-scoped authentication and RBAC. Evidence and history tables use composite tenant/person foreign keys.

## Not yet connected

Provider interfaces are in place, but no external registry, licensed data provider, or company-site discovery service is activated in this build. A real connector requires a selected provider, API credentials where needed, contract/terms review, rate limits, and a verified field mapping. No data has been harvested by this build.
