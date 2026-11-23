# Delegation and blind selection implementation

Scope: implement approved automatic delegation and request-local blind team selection using the existing team executor. The separate quarantine and native-UI proposals in the design and builder review are preserved and are explicitly not implemented here. Brainstorming is already approved.

1. Inspect actual settings and planner schemas, permissions and request execution; retain existing human model settings and runtime provider identities.
2. Add failing mocked/source regression tests for opaque request-local selection, safe invalid-ID errors, sanitization of configured comments, and pre-work default-on routing with sticky refusal.
3. Map opaque IDs privately to configured models at the host boundary. Selection prompts/schemas/examples/defaults/fallbacks must contain only opaque IDs and sanitized capability/use comments. Do not claim inference-proof anonymity or proven bias causality.
4. Route small requests directly and large multipart requests through existing teams before substantive work, with a concise visible reason. Preserve approvals, limits, cancellation, partial handoff, and no nested teams. Require explanation and permission before consequential solo fallback after required-team failure; never automatically retry refusal within a request.
5. Run targeted red/green tests, update Unreleased notes and delegation documentation, rebuild host data, and run the full pytest collection in bounded disjoint batches. Store verification manifests/results only in `.agentx/verification/`; run `git diff --check`. No commit before independent review.

Conservative fallback: if the complete scope cannot fit, finish a coherent verified slice and report exact remaining requirements rather than claiming complete coverage.
