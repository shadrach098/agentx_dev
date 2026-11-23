# Trust and delegation design

Date: 2026-11-21
Status: Proposal; consequential choices below require owner approval before implementation.
Scope: Three independently acceptable proposals. This is not an implementation plan or a claim of shipped behavior.

## Intent and recovered context

Make delegation reliable without expanding authority, inspect downloaded skills before activation, and make browser setting controls native actions rather than synthetic chat sends. No product code changes are authorized by this document.

Recovered research: `.agentx/team/2026-11-21-192557-create-a-concrete-implementation-design-only/01-delegation-designer@gpt-6.2-astra.md` and `02-delegation-design-reviewer@gpt-6.2-sol.md`. Their routing proposals inform this document, but their historical line numbers are not current evidence. The subsequent independent review was BLOCKED because no design or review prompt existed; that was not substantive approval.

User reports about unreliable delegation and unwanted setting messages motivate this work, but are not measured failure rates. Suggested policy defaults below are proposals, not verified current defaults.

## Verified baseline

Source inspected at `9b6042f`; references describe that checkout, not proposed behavior.

| Area | Evidence | What it establishes |
| --- | --- | --- |
| Delegation guidance | `agentx_dev/cli/context.py:49` | Instructions request teams for large multipart work and direct execution for small/medium work. Instructions alone are not an enforced routing decision. |
| Existing executor | `agentx_dev/cli/app.py:337,1447-1488` | Team tool is gated; existing execution returns results to the parent and accounts for jobs. Reuse it rather than introducing a second executor. |
| Refusal/stall | `agentx_dev/cli/app_team.py:127-133` | Existing checks refuse further jobs on job limit or stalled team. Preserve them. |
| Download/extraction | `agentx_dev/Skills/Install.py:35-44,119-147,169-199` | Existing byte/count limits, HTTPS redirect restrictions, path checks and link handling already exist. Installation is not unvalidated today. |
| Activation | `agentx_dev/cli/app.py:1375-1419` | Current flow previews, obtains approval, installs, and reloads. Durable quarantine plus approval bound to inspected bytes would be additions. |
| Browser pickers | `agentx_dev/resources/webui/app.js:965,977` | Model and approval-mode pickers send synthetic slash commands. This does not establish the same behavior for every setting or prove model-history pollution. |
| Host send path | `agentx_dev/cli/webui.py:941-970` | Send dispatch handles commands, busy state, and conversation selection. New actions must preserve those boundaries. |
| Native settings | `agentx_dev/cli/webui.py:1285-1349,2248-2249` | A native settings API already exists. Persistent defaults differ from live conversation changes; folder settings cannot enable accept-edits/auto. Declined project allow rules remain declined. |

## A. Reliable delegation through the existing team infrastructure

Implementation update (2026-11-21): the owner approved automatic delegation and request-scoped blind team-model selection only. The separate quarantine and native-UI proposals below remain proposals and are explicitly excluded from this implementation. See `../plans/2026-11-21-delegation-blind-selection.md` for the implementation sequence.

The first bounded slice adds a private opaque-ID roster with sanitized configured capability/use comments and strict identity-free errors for invalid selections. It is not yet wired into team prompts or execution: current runtime selection is not claimed anonymous. Existing `AgentSpec.model` is a string field, so opaque selection can use that schema without rewriting deliberate human model configuration. Configuration comments must avoid provider/model identities and identifying aliases; conservative removal of known configured labels and common identity tokens cannot promise inference-proof anonymity. No causal claim about model bias is established.

New live-team requirement: messages typed during a job must reach the supervisor for triage, task updates or new delegation, rather than being injected directly into whichever helper is running. Human-facing running helper rows must show the approved runtime model identity; this is distinct from the LLM selection boundary. Both live-input changes and default-on pre-work routing remain to be implemented and verified in subsequent slices.

### Contract

Introduce a host-owned routing decision per user request before a substantive answer or consequential work. Prompt text remains guidance, not the enforcement point. The host decision chooses direct work, existing team execution, or a visible blocked/deferred outcome. A model assessment may advise complexity but cannot grant tool authority, override refusal, or invoke a new executor.

A bounded assessment, if selected, is tool-free, receives only the minimum request/context summary, has a strict response schema, token and wall-clock limits, and at most one attempt per routing decision. Malformed output, timeout, or unavailability yields an explicit reason and the configured fallback, never an unbounded assessment loop. Exact numeric budgets and fallback policy require approval below.

Recheck the decision at the response boundary: a model cannot bypass a required team route by immediately emitting a final answer. Read-only discovery needed to establish scope must be bounded and visible, not an unlimited direct-work loophole. Small and medium work should remain direct where policy permits.

Reuse current team approval, model selection, job accounting, cancellation, partial-result handoff, and refusal/stall checks. A route decision is not approval to run a team. User refusal is sticky for this request across retries and partial results: do not rephrase or automatically retry the refused job. New user authorization is required to reconsider it. A stall or exhausted job budget produces a truthful partial result or permitted direct fallback, not a silent reset of counters. Do not create nested teams from helpers.

Record the route, reason, assessment outcome, approvals, attempts and terminal outcome without recording secrets or unnecessary request contents. Show useful progress and distinguish refused, unavailable, stalled, cancelled and complete outcomes.

### Consequential choices requiring approval

- Policy surface and default: e.g. off/advisory/automatic; whether a user can require teams, and precedence of session, user and project preferences. Project content must not expand approval authority.
- Eligibility threshold and allowed discovery budget; assessment model, token/time budgets, privacy/cost disclosure, and deterministic versus model-assisted routing.
- Fallback when assessment or team infrastructure fails: stop, ask, or direct work within existing permissions. Recommendation: never silently do consequential direct work where a team was explicitly required.
- Scope of sticky refusal beyond one request. This proposal requires request-local stickiness; broader persistence needs an explicit user-facing decision.

### Acceptance boundaries

A multipart request cannot bypass required routing by answering immediately. Decline, stall, cancellation, malformed assessment and repeated partial results terminate within configured budgets. Direct and delegated work retain identical approval limits. Small requests do not incur mandatory helper work unless explicitly requested.

## B. Quarantined, inspected skill installation

### Trust boundary and lifecycle

Treat archive contents, metadata, skill instructions and inspection output as untrusted data. Proposed lifecycle: downloaded -> bounded extracted quarantine -> inspected -> explicitly approved exact candidate -> atomically activated, or rejected/expired. Quarantine is outside all skill discovery, prompt-loading and executable search roots. No content becomes an active skill merely because it downloaded or passed inspection.

Preserve existing download and extraction bounds and redirect rules. Apply cumulative compressed/download and expanded byte limits, per-file size, entry count, path depth and processing-time limits throughout processing, not after extraction. Reject traversal, absolute/drive/UNC paths, links/reparse points, special files, duplicate normalized paths and case/Unicode collisions where the target filesystem aliases them. Never follow links in extraction or promotion. Reject unsupported archives rather than weakening checks. Exact added limits and supported formats need approval; existing limits must not increase incidentally.

The quarantine record binds origin, resolved source/revision where available, archive digest, normalized file manifest and content digests, inspection version, limitations and result to an immutable candidate identifier. Inspect local quarantined bytes, not a freshly downloaded substitute. Any changed bytes, manifest, source identity or applicable inspection result invalidate activation approval and require reinspection and approval. Protect against concurrent mutation at the trusted promotion boundary; path-name equality alone is insufficient.

Inspection is static and advisory. It may identify suspicious instructions, network references, scripts, binaries, secrets or unsupported content; it cannot certify absence of prompt injection or other malicious behavior. Render suspicious text as quoted data, not instructions. An optional model inspector has no tools, credentials or execution authority, receives bounded content, and may not turn its verdict into approval or new permissions. Truncation, skipped files and inspection failure are visible incomplete results, never a clean bill of health. Inspection must not install dependencies, import modules, execute scripts, render active documents, follow content-supplied URLs, or run untrusted skill code.

Approval shows exact identity/digest, origin, inventory, findings, incomplete coverage and the destination/overwrite effect. A trusted user's explicit activation approval is separate from permission to download or inspect. Neither a clean report nor an agent's interpretation can approve activation or grant tool privileges. Existing runtime permission gates remain in effect after activation; approval does not make future skill instructions trusted.

Promotion verifies the approved candidate immediately before an atomic same-filesystem activation. Stage on the destination filesystem when necessary; cross-volume copying is not atomic. Serialize conflicting activations, preserve the last usable version and change discovery only after successful publication. On crash, failure or reload error, leave the previous version usable or restore it with an explicit failure outcome; never expose a partial tree. Rollback cannot silently activate an unrelated or unapproved candidate. Platform-specific guarantees must be validated before claiming atomicity.

### Consequential choices requiring approval

- Quarantine location, ownership, retention duration, storage/count caps, deletion and cleanup UX; abandoned/rejected candidates remain inactive and bounded.
- Added extraction limits and formats; behavior for unsupported files, incomplete inspection and warnings (recommend block activation on extraction/integrity failures, never call incomplete inspection safe).
- Whether model-assisted inspection is offered at all, its provider/data disclosure and numeric budgets; a local static-only path must remain possible if offered as a product commitment.
- Activation approval UX and overwrite scope; backup retention, rollback consent and crash-recovery guarantees on supported Windows and Unix filesystems.

### Acceptance boundaries

Traversal, aliases, links, oversized/overcount archives and concurrent mutation cannot escape quarantine or bypass limits. Changed bytes cannot reuse approval. Rejection, inspection timeout and interrupted promotion leave no discovered candidate and preserve the prior active skill. Tests use inert fixtures; no untrusted code runs to prove these properties.

## C. Native browser setting actions

Replace only the verified model/mode picker synthetic sends with typed, validated host actions. Preserve terminal slash commands and the existing native persistent-settings API. Route both terminal commands and browser actions through shared semantic operations where appropriate; do not simulate a chat send internally.

Actions identify the intended conversation/session, action kind and validated value; host authorization and session ownership checks remain authoritative. Reject stale, missing or mismatched conversation identities rather than changing another device's active session. Preserve request-origin/authentication protections of the existing API. Do not let project settings or lower-trust content relax permissions. A live mode change must have the same deliberate-user semantics as the existing picker; it is not an agent-granted privilege.

Return structured success/error and authoritative current state. Preserve the distinction between live conversation model/mode and saved next-start defaults. Preserve existing permission recalculation and declined project rules. Busy-state behavior must be explicit and consistent with existing command behavior; rejecting with feedback is preferable to silently applying a setting to an in-flight operation. Changes must not create synthetic user/assistant messages, trigger model generation, or consume a user-turn budget. A settings audit event may exist separately from chat history and must not be supplied as a user prompt.

Keep keyboard operation, labels, focus and screen-reader status/error announcements. Optimistic control state must roll back on rejection; all connected views receive authoritative updates. Avoid exposing sensitive setting values in logs or accessible error text.

### Consequential choices requiring approval

- Exact busy-state policy (reject, defer explicitly, or apply at a defined boundary), and stale-session/version conflict handling.
- Whether to retain an informational UI-only activity event, its retention, and where it appears; not a synthetic conversational message.
- Live model-change persistence remains distinct from saved defaults; any change to that distinction requires separate approval.

### Acceptance boundaries

Model/mode clicks reach the correct session without synthetic chat messages or model calls. Invalid values, unauthorized clients, stale sessions, busy runs and failed changes produce accessible feedback and no hidden mutation. Existing native settings and slash commands retain their permission and persistence semantics.

## Minimal separable phases, not an implementation plan

1. Confirm each proposal and the consequential choices independently. No proposal depends on accepting the other two.
2. Establish the selected contracts and boundary acceptance checks for that proposal before enabling it.
3. Introduce that proposal in a limited, observable rollout with an explicit disable/recovery path, then evaluate outcomes before expanding it.

These are design gates, not ordered coding tasks, patch instructions or authorization to implement. No implementation plan, product changes or untrusted execution are included. No changelog entry is warranted for unshipped proposal behavior.

## Review and handoff

Review assignment and record: `docs/superpowers/specs/2026-11-21-agentx-trust-and-delegation-builder-review.md`.
The earlier missing-artifact review was not substantive approval. A subsequent independent reviewer read both on-disk artifacts and returned **PASS as a proposal for owner decision; NOT ready for implementation approval**, identifying no additional concrete design blockers beyond the explicitly reserved owner choices. The review record separates that independent finding from the author's bookkeeping resolution; the final bookkeeping edits have not received a further independent review.

Routing obligation and failure behavior, warning-bearing or incomplete skill activation, quarantine and recovery guarantees, and busy/stale browser actions remain unresolved owner decisions, not approved defaults or delegated implementation discretion. Internal schema names, component boundaries and exact accessible feedback wording may remain builder choices within the stated contracts. The optional informational activity event need not block the other independently acceptable proposals. Proposal acceptance does not resolve these decisions or authorize implementation.
