# ADR-002: Bring your own key, multi-provider through the LiteLLM SDK

- Status: accepted (2026-10-28)

## Context

Teams want to choose the model and keep billing and data agreements with their own provider, not
depend on a personal subscription or a single vendor.

## Decision

- Credentials are repository/organization secrets passed as action inputs; PullBeacon never stores
  or logs them, and never uses personal OAuth subscriptions.
- One `LLMClient` protocol with a `LiteLLMClient` built on the LiteLLM **Python SDK** (no proxy,
  no database) and a deterministic `FakeLLMClient` for tests and mock runs.
- Provider + model id are configuration (`anthropic/…`, `openai/…`, `gemini/…`); no model id is
  hard-coded in the engine. Placeholders are rejected.
- The model receives no tools. Output is JSON validated by PullBeacon.

## Consequences

- Switching provider is a config change; the same validation applies to every provider.
- Behaviour differences between providers (schema support, parameters) are handled by a
  structured-output fallback and `drop_params`; only one provider can be verified live per setup,
  the others are covered by mocks until keys are available.
- LiteLLM is a large dependency with a past supply-chain incident; it is pinned by lockfile and
  imported lazily.

## Alternatives rejected

Provider SDKs directly (three code paths to maintain); a LiteLLM proxy (extra service and state).
