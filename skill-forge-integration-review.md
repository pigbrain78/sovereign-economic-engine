# Skill Forge Integration Review

## Source inspected

`pigbrain78/skill-forge` was inspected at commit depth 1. It contains a React/Vite control surface, TypeScript domain types, seeded skill examples, and an `artifact-review-integration` skill manifest. It does **not** contain a backend persistence layer, executable skill registry, trusted evaluator, wallet connector, or signed authority implementation.

## Implemented

- Adapted Skill Forge's `SkillGenome`, permissions, governance, validation report, and lifecycle status concepts into `app/skill_forge.py`.
- Added deterministic SHA-256 skill identity hashing over strict canonical JSON.
- Added fail-closed validation: malformed genomes reject; failed reports remain `testing`; security issues reject; scores below 90 remain `testing`; unverified SAIL approval remains `testing`; only complete evidence can yield `approved`.
- Added API persistence for skill genomes, validation reports, lineage, and execution logs in the canonical SQLite backend.
- Added a proposal bridge that creates an ordinary Pocket OS proposal only after an approved validation report. The bridge never grants execution authority or purchases a tool.

## Not merged

- Seeded UI metrics and predictive simulation outputs: they are presentation examples, not runtime evidence.
- Marketplace downloads, reputation, adoption, and pricing claims: no trusted source or settlement contract exists.
- Automatic mutation/promotion: remains behind existing sandbox promotion and human-ratified upgrade paths.
- SAIL approval claims from seeded artifacts: accepted only when explicitly supplied to the backend and still subject to evidence checks; not inferred from the source repository.

## Residual blockers

- A production evaluator needs a hardened sandbox with measured resource usage and artifact-bound receipts.
- External tool discovery needs a provider adapter with authenticated namespace and budget reservation.
- Skill execution needs capability grants, signed evidence, replay protection, and durable multi-process settlement.
- Local SQLite remains single-process development persistence, not replicated production storage.
