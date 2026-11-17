# Security, privacy and LGPD baseline

1. Define and document purpose and lawful basis per processing activity before enabling personal-data enrichment.
2. Collect only professional data necessary for the declared purpose; do not collect sensitive personal data.
3. Record source, collection time, provenance and verification status for each material field.
4. Implement retention schedules, correction/deletion workflows, access controls and data-subject request handling.
5. Maintain tenant isolation in API authorization, data access and background jobs; never accept an untrusted tenant_id as authorization.
6. Use secrets management; never commit credentials or real personal data to fixtures.
7. Website fetchers must block loopback, private, link-local and metadata IP ranges; validate DNS and redirects at every hop; enforce content-type, size, time and redirect limits.
8. Respect provider terms, applicable law, rate limits and source access controls. Public availability is not a blanket license for reuse.
9. Require human review and applicable permission/legal checks before outbound contact. Enforce suppression and opt-out lists.
10. Log administrative changes and external side effects without unnecessarily logging personal data or secrets.
11. Use synthetic data in tests and disposable databases; do not run destructive tests on preserved environments.
12. Conduct a security and privacy review before production launch; obtain qualified Brazilian legal advice for the actual processing design.
