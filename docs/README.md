# Documentation map

The repository keeps public project documentation separate from operating
procedures, source-evidence material, and immutable historical records.

## Start here

- [Architecture](ARCHITECTURE.md)
- [Local development](LOCAL_DEVELOPMENT.md)
- [Agent evaluation baseline](agent-evaluation-baseline.md)
- [Prompt-injection defence](prompt-injection-defense.md)

## Operations

[`operations/`](operations/) contains the current runbooks for PDF evidence,
question-bank review, and Route 2 imports.

## Evidence

[`evidence/`](evidence/) contains source-document registrations, inventories,
audits, and candidate material used to make textbook evidence traceable. Some
runtime configuration reads `evidence/garble_audit.csv`; do not remove it.
