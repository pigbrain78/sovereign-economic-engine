# Pocket OS Treasury

A responsive Cognitive Cockpit for the Sovereign Economic Engine. It presents project context, Shadow observations, evidence-backed proposals, human decisions, wallets, memory, and append-only ledger state without inventing authority or financial state.

## Run

```bash
pnpm install
pnpm run dev
```

Set `VITE_API_BASE_URL` to the FastAPI service URL for another environment. The Settings page also stores a browser-local override.

## Boundary

- Shadow is advisory: `AUTHORITY: NONE`, `CAN EXECUTE: NO`, `CAN RATIFY: NO`.
- Proposal approval records a human decision only; it does not purchase, reserve, or authorize execution.
- Wallet, memory, health, and ledger displays are read-only canonical backend responses.
- Unsupported backend capabilities render explicit unavailable states instead of fabricated data.

## Hugging Face model layer

The Agents, Tool Discovery, and Evaluations views use the backend Hugging Face catalog. They show interchangeable model candidates, capabilities, provider options, quality/latency estimates, pricing verification state, and a budget-aware quote lab. The UI never receives the Hugging Face token and never treats a quote as execution authorization.

The Wallets view displays operating, development, safety, and owner reserves plus upgrade proposals. Upgrade proposals remain pending until evaluation evidence and a verified human approval signature are recorded by the backend.
