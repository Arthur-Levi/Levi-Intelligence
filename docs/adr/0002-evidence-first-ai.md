# ADR 0002: Evidence-first AI outputs

- Status: Accepted
- Date: 2026-11-16

## Decision
AI agents may summarize, classify and recommend, but material factual claims must point to source evidence and carry verification state. Inferences are labeled as inferences. Missing evidence remains unknown; it is never filled by model invention. Scores are explainable and versioned.

## Consequences
- Source/evidence objects are first-class domain entities.
- Diagnostic generation consumes evidence bundles, not ungrounded prose.
- Human review is required before a diagnostic is sent externally.
