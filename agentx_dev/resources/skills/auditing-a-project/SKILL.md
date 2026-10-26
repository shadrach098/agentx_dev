---
name: auditing-a-project
description: How to audit or review a whole project (security, bugs, risks) - map it, run its checks, split the reading by area, verify, then report by severity.
tools: [start_team_job, read_path]
version: 2
---
# Auditing a project

An audit is a report of what is wrong and how badly, with proof. Work in this order and keep each part short.

1. **Map it yourself first.** List the root, read the README and the manifest (package.json, pyproject.toml),
   and find the entry points: routes or API handlers, auth, payments or money, database access, admin pages,
   background jobs, webhooks. Note the stack. Do not read every file.
2. **Run its own checks** with the commands the project already has: typecheck, lint, unit tests, a dependency
   audit (`npm audit --omit=dev`, `pip-audit` when installed). Do not install anything, and skip tests that need a
   database, network or secrets. Record what passed, what failed, and what could not run.
3. **Split the reading by area.** For a project with more than a few files, use one `start_team_job` with one
   explorer step per area and no `depends_on` between them, so they run side by side:
   - security: auth and sessions, access checks on every route, input validation, secrets, file uploads;
   - money and data flows: payments, webhooks, bookings or orders, what is written to the database and when;
   - frontend and admin: what the browser can call, admin-only actions, forms, consent and privacy.
   Tell every step to report each finding as `file:line`, severity (high / medium / low), and the concrete way it
   fails ("two clicks on Pay create two charges"). Ask for what is done well too.
4. **The checklist** each area goes through where it applies:
   - an external service that is down or slow (payment, email, storage): what the user sees, what is left half done;
   - client IPs from `X-Forwarded-For` trusted without a known proxy (rate limits and logs can be faked);
   - request size limits on bodies and uploads;
   - idempotency: a retried webhook or a double submit must not charge, book or send twice;
   - races and holds that never expire (two users booking the last seat, a reservation held forever);
   - brute force on login, codes and tokens (rate limits, lockout);
   - consent and personal data (cookies, emails, what is stored and for how long).
5. **Verify before you report.** Open the cited lines of every high and medium finding yourself. Drop or
   downgrade what the code does not support. A wrong finding costs more trust than a missed low one.
6. **Report** in this shape:
   - **Fix first** - high severity, each with `file:line`, the failure, and the fix in one line;
   - **Medium** and **Low** - the same, shorter;
   - **Done well** - what is already solid, so it is kept;
   - **Checks** - what ran (typecheck, tests, audit) and the results;
   - **Test gaps** - the risky paths that no test covers.

**As a team helper** with one area: do steps 4 and 5 for your area only, then report your findings in the shape of
step 6 (and what you did not get to). Do not look for this skill's file on disk; this text is all of it.

Keep the whole report in one answer. If the team's result was cut, ask the team again only for what is missing.
