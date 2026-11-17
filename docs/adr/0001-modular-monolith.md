# ADR 0001: Start as a modular monolith

- Status: Accepted
- Date: 2026-11-16

## Context
The platform needs several bounded capabilities and asynchronous agents, but early traffic and team size do not justify distributed microservices. Premature service separation would increase deployment, observability and data-consistency costs.

## Decision
Start with a modular monolith API and independently scalable worker processes. Keep domain modules and integration adapters behind explicit interfaces. Use PostgreSQL as the system of record and Redis for transient queue coordination. Extract services only when operational evidence justifies it.

## Consequences
- Simpler local development and transactional workflows.
- Clear module boundaries remain mandatory.
- Long-running research and AI tasks never execute inside request handlers.
- Workers must be idempotent, tenant-aware and observable.
