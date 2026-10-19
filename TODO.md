# Pocket OS Treasury build tracker

- [x] The Tool Discovery cockpit page reads `GET /tools` from the canonical backend, supports verified/unverified filtering, shows explicit unavailable/empty states, and displays provider, source, capabilities, permissions, manifest digest, proposal eligibility, and `execution_authorized: false` without inventing catalog records.
- [x] The Sandbox Evidence cockpit page reads `GET /upgrades/proposals/{proposal_id}/promotion`, displays verification, promotion, rollback, candidate hash, lifecycle, canary percentage, and explicit non-authority messaging, and keeps historical evidence read-only.
- [x] The route manifest includes `/sandbox` and remains synchronized with the frontend page set.
- [x] Backend compilation, full pytest suite, frontend TypeScript check, and production build pass for this milestone.
- [ ] Replace the reference promotion layer with a real isolated OS/container executor and production signed-artifact trust-root rotation before allowing generated code to run.
