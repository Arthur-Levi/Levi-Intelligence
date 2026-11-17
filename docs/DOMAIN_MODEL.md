# Core domain model (target)

- Tenant: customer workspace and billing boundary.
- User / Membership: identity and role within a tenant.
- ICPProfile: sector, geography, size, exclusions and fit criteria.
- ProspectingRun: user-authorized discovery job, source, parameters and status.
- Organization: company-level record, normalized domain and deduplication keys.
- SourceRecord: immutable provenance record (source, URL/reference, collected_at, terms/purpose metadata).
- Evidence: atomic claim with value, source_record_id, verification status, confidence and observed_at.
- Person: professional contact only where collection and use are justified; data minimization applies.
- OrganizationPersonRole: relationship and role evidence, not assumed authority.
- WebsiteAudit: crawl scope, timestamp, technical observations and limitations.
- Opportunity: tenant-owned commercial fit assessment and lifecycle status.
- ScoreBreakdown: versioned rubric and criterion-level evidence.
- Diagnostic: generated artifact, template version, evidence references and human review state.
- CRMDeal / Activity / Task: pipeline and sales operations.
- SuppressionEntry: tenant-level do-not-contact and objection state.
- AuditEvent: append-only record of privileged and external side-effect actions.

Every tenant-owned row carries tenant_id. Repository/service boundaries must require an authorized tenant context; database row-level security is an additional defense, not the only authorization control.
