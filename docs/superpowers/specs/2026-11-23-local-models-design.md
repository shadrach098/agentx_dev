# Local models: design

Status: draft for review. Target release: 3.10.0.

## Why

People run models on their own machine with Ollama, LM Studio or llama.cpp. A local model costs nothing per
token, keeps the code on the machine, and needs no account. Today agentx_dev talks only to OpenAI and Anthropic,
and the `agentx` app refuses any model name that does not start with `claude` or `gpt`.

There is a second reason. When a paid model stops working mid-job (no key, a rejected key, the account out of
credit, the provider unreachable), a long team job stops with it. A local model is a backup that is always there.

Both reasons apply to library users and to the `agentx` app.

## What we learned first (2026-11-23, on the requester's laptop)

Ollama 0.40.17, models `qwen3:8b` and `qwen3-agent` (the same 8B weights with `num_ctx 16384`).

| Test | Result |
|---|---|
| Plain chat on Ollama's OpenAI-compatible address `http://localhost:11434/v1/chat/completions` | Works. Qwen's thinking arrives in a separate `reasoning` field on the message; `content` is clean. |
| One request with one tool | Works: a clean `tool_calls` entry, `finish_reason: "tool_calls"`. |
| `AgentRunner(model=GPT(base_url=..., use_responses_api=False), agent=ReAct)`, file tools, default settings (as agentx runs today) | **Fails.** One text-only turn, then a final answer with empty content, reported as outcome `done`. No tool was called. |
| The same with `bind_tools_natively=True` | **Works.** Read the file, answered correctly, wrote the new file. 28 seconds. |
| Context window | `qwen3:8b` is loaded with 8,192 tokens (Ollama's default) although the weights take 40,960. `qwen3-agent` asks for 16,384. When a request is longer than the window, Ollama silently drops its start, which is where the instructions are. |

So the transport needs no new client: the existing `GPT` class pointed at the local address, using chat
completions only, does the talking. What is missing is native tool binding for small models, a check that a
local model works, context-window awareness, the fallback, and the app's side (names, menu, settings).

## Goals

1. **Library: a `Local` chat model.** `Local(model="qwen3-agent")` talks to any OpenAI-compatible server,
   `http://localhost:11434/v1` (Ollama) by default, `base_url=` for others (LM Studio's `http://localhost:1234/v1`,
   llama.cpp's server). It is a `GPT` subclass that:
   - uses chat completions only (never the Responses API);
   - needs no key (sends a placeholder; a server that wants one gets `api_key=`), and never reads
     `OPENAI_API_KEY` (that key must not be sent to a local or third-party server);
   - counts tokens from the server's `usage` like any model (the framework has no price table, so there is no
     cost to zero);
   - reads the thinking that local servers put in a `reasoning` (Ollama) or `reasoning_content` (LM Studio, vLLM)
     field and returns it the way GPT's Responses path and Claude already return `"reasoning"`;
   - asks the runner for native tool binding (an attribute the runner reads, so `AgentRunner(model=Local(...))`
     binds natively without the user knowing the flag; an explicit `bind_tools_natively=False` still wins);
   - with no `model`, uses the server's only model, and when the server has several, raises an error that names
     them;
   - offers `Local.models(base_url=...)` (the names the server lists) and `check()` (see goal 4) for anyone who
     wants to test before use.
2. **Library: fallback.** A way to say "use this model, and if it fails for good, use that one": the primary is
   any chat model, the backup is any chat model (normally a `Local`). Details in **Fallback** below.
3. **Library: an empty final answer is never passed off as an answer.** Two cases, for every model:
   - The run called no tool and gave no answer (no text, no structured output): it did nothing. The outcome is
     not `done`; it says the model gave no answer. This is the case the spike hit.
   - The run did tool work and then gave an empty answer: the outcome stays `done` (the work happened, and a team
     step's dependants still run, as today), but the answer is replaced by a short note: "The model gave no final
     answer. It called: read_path, write_file (files changed: done.txt)." The note is built from the run's tool
     calls, in the same way team jobs already list changed files.
   Nothing else changes for Claude and GPT.
4. **A check before use.** Checking a local model means: the server answers, the model is in its list, and one
   tiny request with one tool comes back as a tool call. On Ollama it also reads the window the model is loaded
   with (see **Context window**). The check reports a short reason when it fails ("Ollama is not running at
   localhost:11434", "no model named qwen3-agnet; the server has qwen3:8b, qwen3-agent", "the model answered in
   text instead of calling the tool: it may not support tools"). The first request can take a while because the
   server loads the model; the check waits up to two minutes and says it is loading.
5. **agentx: pick a local model like any other.** Local models appear in the model menu under the message box,
   in the Settings model field's suggestions, and in `/model`, marked as local (`qwen3-agent · local`). Choosing
   one runs the check; on success agentx switches, on failure it stays on the current model and shows the reason.
   `agentx --model qwen3-agent` and `"model": "qwen3-agent"` in a settings file work too.
6. **agentx: local helpers in team jobs.** A local name in `teamModels` / `/team-models` gives helpers on that
   model (`coder@qwen3-agent`), counted in the same limit of 4 extra models, with notes and auto notes as today.
7. **agentx: fallback, automatic and marked.** When the paid model fails for good, agentx switches to the
   fallback model, marks everything that ran on it, and keeps going. Details in **Fallback**.
8. **Docs.** The guide, What's new, Upgrading, the cookbook (a recipe for Ollama, one for LM Studio, one for the
   fallback) and the README cover it.

## Non-goals

- Installing, pulling, starting or stopping models or servers. agentx uses a server that is already running.
- Writing Modelfiles or changing a model's settings on the server. agentx says what to change; the user does it.
- Ollama's own chat API. Chat goes through the OpenAI-compatible address only. Ollama's read-only `/api/ps` and
  `/api/show` may be used to learn the window size; nothing else.
- Embeddings from local servers (a later release).
- Falling back from a local model to a paid one. Never, in any form (see **Anti-goals**).

## Anti-goals (would be a failure even if it works)

- **Silent downgrade.** Work done by the fallback without a visible mark, in the conversation, in a team job's
  result, or in what the main model is told.
- **Code leaving the machine against the user's choice.** A conversation on a local model never sends anything to
  a cloud provider **on its own**: no fallback to cloud, no "auto note" or summary call to a cloud model while the
  chat model is local (those calls use the chat model, which is local). What the user chose explicitly stays
  allowed: cloud helpers they listed in `teamModels`, and voice input they start themselves. When a team job on a
  local chat model will use a cloud helper, the job's approval says so ("helpers on claude-sonnet-5-5 will see
  this project's files").
- **A cloned project redirecting your prompts.** A project's `.agentx/settings.json` must not be able to set the
  local server's address: that would send your conversation, files included, to a server of the project's
  choosing.
- **A slower start for people without a local server.** Looking for a local server must not delay startup or the
  first prompt.
- **Instructions silently cut off.** A request longer than the model's window must not go out as if nothing were
  wrong.

## Constraints

- Python 3.10+, no new dependencies (the `openai` package already speaks to these servers).
- Windows, macOS and Linux.
- Tests run without any local server: a fake OpenAI-compatible server or a stubbed client.
- Nothing that works today changes: a `claude*`, `gpt*` or `o<digit>*` name means what it means now.
- The project's rules for settings files hold: a project file is untrusted and may not set anything that sends
  data elsewhere or loosens approvals.

## Names: which models are local

agentx decides by name, as today: `claude*` is Anthropic, `gpt*` and `o<digit>*` are OpenAI, and **any other
name is local** (asked of the local server). A typo such as `cluade-opus-5-5` therefore becomes a local name and
fails the check with "the local server has no model named cluade-opus-5-5"; the message also says that Claude
names start with `claude`. No prefix is needed, so `qwen3-agent` and `llama3.1:8b` are typed as the server lists
them.

## Settings

Two new keys, both in the user's own settings file (`~/.agentx/settings.json`) and the Settings form:

| Key | Default | Meaning |
|---|---|---|
| `localServer` | `http://localhost:11434/v1` | The OpenAI-compatible address of the local server. |
| `fallbackModel` | none | The local model to switch to when the chat model or a helper's model fails for good. Empty means no fallback. A `claude*`, `gpt*` or `o<digit>*` name is refused when saved ("the fallback must be a local model") and ignored with a warning if found in a file. |

- A project file may name a local model as its `model` (a name is harmless) but may **not** set `localServer`
  or `fallbackModel`; either is ignored there with the usual one-line warning.
- An address that is not on this machine (not `localhost`, `127.0.0.1` or `::1`) is allowed, for a server on
  another computer of yours, and agentx says once at startup that conversations go to that host.
- For the requester's machine: `"fallbackModel": "qwen3-agent"` and `"model"` unchanged. `qwen3-agent` is their
  own variant, so no code or default anywhere names it.

## Finding the local server

At startup agentx asks the local server for its models **in the background**, with a short timeout (a fraction of
a second for a refused connection; agentx never waits on it before the first prompt). The answer fills the model
menu and the Settings suggestions. No answer means no local models in the menu and nothing else; typing a local
name still runs the check then.

The first time agentx finds a local server and `fallbackModel` is not set, it offers once, as a notice with a
yes/no in the app window and a line in the terminal: "Ollama is running with qwen3-agent, qwen3:8b. Use one as
the fallback when Claude or GPT fails?" A "no" is remembered and not asked again.

## Context window

A small window is the most likely way a local model fails quietly. Warning is not enough: one agentic turn that
reads a few files fills 16,384 tokens, and every request after that would be cut. So agentx **keeps every request
to a local model inside its window**, and warns only when it cannot.

**Knowing the window.** On Ollama, the check reads the window the model is loaded with (`/api/ps` for a loaded
model, else `num_ctx` from `/api/show`, else Ollama's default of 8,192). Other servers: an optional
`"localContext": 16384` in the user's settings; without it, agentx assumes 8,192. Requests are measured in tokens
estimated from characters (the server's `prompt_tokens` after each request corrects the estimate for that model).

**A budget per request**, in this order, each step only if the one before was not enough:

1. **agentx's own instructions fit first.** For a local model, `AGENTX.md` and the project log are shortened to fit
   a share of the window (at most a third of it) instead of today's caps (20,000 and 6,000 characters), with a
   line saying they were shortened. The base prompt and tool definitions are never cut.
2. **Old tool results shrink.** Within a turn, the oldest tool results are replaced by one-line stubs ("read_path
   notes.txt: 1,240 characters, shown earlier"), newest kept whole.
3. **The conversation is summarised, during the turn if need be.** The existing summary (`cli/compact.py`) runs
   when the conversation passes about 60% of the window, instead of today's 60,000 characters (`THRESHOLD_CHARS`,
   `cli/compact.py:15`), and may run between two steps of a turn, not only before a turn. Its own input is capped to
   fit the window (today `MAX_INPUT_CHARS` is 60,000 characters, about 15,000 tokens: more than an 8k window).
4. **If the instructions and the newest message alone do not fit,** the request is not sent. The turn stops with:
   "qwen3:8b's 8,192-token window is too small for this conversation. A variant with a larger window fixes it" and
   the Modelfile example from the guide (`PARAMETER num_ctx 16384`).

**Switching to the fallback mid-conversation** goes through the same budget: a long Claude conversation is
summarised to fit the local window before the local model sees it.

**One warning** per conversation, when steps 1-3 had to act, so the user knows the model is working with less
than the whole conversation.

## Fallback

**When it switches.** Only on failures that waiting will not fix:

| Failure | Falls back? |
|---|---|
| No key for the provider at startup | Today's offer comes first: when the other paid provider's key is set, agentx offers that model, as now (`other_provider`, `cli/models.py:50-58`). Only when that is not possible, or declined, does agentx start on the fallback and say why. A team helper whose provider has no key is left out at job start as today ("Not used: ... (no key)"); that is not a fallback. |
| Key rejected (401, 403) | Yes |
| Out of credit or quota (OpenAI `insufficient_quota`, Anthropic's billing errors) | Yes |
| The model name unknown to the provider (404) | Yes |
| Unreachable or 5xx **after** the usual retries are used up | Yes |
| Rate limited (429 that is not a quota error), overloaded, a timeout that a retry fixes | No: retried as today |
| A request the provider refuses, a bad request (400), a tool or code error | No: it would fail again |

The builder decides the exact classification from the error types both SDKs raise; the table is the intent.

**Which way.** Paid to local only. If the chat model is local and fails, nothing else is tried: the turn fails with
the reason, as today.

**The turn that was running.** It carries on from where it stopped; it is never started again, so an edit or
command the user already approved does not run twice. agentx keeps the stopped turn (as it does today for an
out-of-credit error, `_keep_stopped_turn`), rebuilds the runner on the fallback (a local model needs native tool
binding, which is fixed when a runner is built), and continues the turn automatically with a note to the model:
the earlier model stopped, here is what it already did, finish the task. Tool calls in the history that the new
runner's mode does not bind (the `ReAct` function) are carried over as plain text, so nothing in the history
refers to a tool the local model cannot see. The history goes through the window budget (**Context window**)
first.

**A team step that was running** runs again on the fallback, but not blind: the new attempt is told what the
failed one already did (its tool calls and the files it changed, the same list `_changed_files` builds for a
helper that ran out of steps) and to continue from there.

**How long.** Once the fallback takes over, it stays for the rest of the conversation (a dead provider is not
tried on every turn). Choosing a model again (menu, `/model`) tries that model afresh. In a team job, the switch
lasts for the rest of the job.

**Marking.** Every place that shows the work says it ran on the fallback and why:

- Conversation: a notice when it happens, "Claude failed: out of credit. This conversation continues on
  qwen3-agent (local). Choose a model to try Claude again." The model chip under the message box shows
  `qwen3-agent · fallback`.
- Team jobs: a step that ran on the fallback says so on its line (`done on qwen3-agent, fallback: claude-opus-5-5
  out of credit`); the result header counts them ("2 steps ran on the fallback model; check their work"), so both
  the user and the main model know to look again. The Details panel in the app window shows the same.
- The work log and saved session record which model each turn ran on.

**Team jobs in detail.** When a helper's model fails for good, that helper is rebuilt on the fallback and the step
is run again on it; other helpers on the same provider switch too (the provider is down for them as well);
helpers on another provider are unaffected. If the fallback also fails, the step fails as today, with both
reasons.

**Library.** The same rules, without the app. It lives in the runner, not in a wrapper model, because switching
to a local model changes how tools are bound and so needs the runner rebuilt and the history carried over:
`AgentRunner(..., fallback_model=Local("qwen3-agent"))`. On a failure from the **When it switches** table the
runner switches for the rest of its life (until `reset_fallback()`), continues the run as described above, emits
an event and a log warning, and records it on `AgentCompletion` (which model finished the run, and why it
switched). A `Supervisor` passes its `fallback_model` to the helpers it builds. agentx uses the same mechanism.
The parameter name and the event's shape are the builder's.

## Thinking

Qwen and other local reasoning models send their thinking in a `reasoning` field. Today the chat-completions path
reads only `content` and `tool_calls`, so that thinking is dropped, and a reply whose `content` is empty becomes an
empty answer. `Local` returns it as `"reasoning"` (goal 1) and agentx shows it as Thinking, as it does for Claude
and GPT. It is never sent back to the model as part of the answer, and a reply with thinking but no content is
still an empty answer (goal 3).

## Native tool binding keeps agentx's instructions

With `bind_tools_natively=True` the runner bypasses the ReAct parser and uses a minimal system prompt of its own.
agentx's instructions (its base prompt, `AGENTX.md`, the project log, the skills list, the team-job rules) must
still reach a local model in that mode, and `strict_tool_dispatch` and `trim_results` must keep working. Native
mode cannot be combined with `use_function_calling`; agentx does not set that today, and the runner must not
switch it on for a `Local` model.

## Where it lands

Facts from `origin/main` at 3.9.0; paths under `agentx_dev/`.

| Area | Today | Change |
|---|---|---|
| Models | `ChatModel.py`: `GPT` (1527) takes `base_url`, `use_responses_api`; chat completions read only `content`/`tool_calls` (`call_with_tools`, 1868-1963). `__init__.py` exports `GPT`, `Claude`. | Add `Local` (a `GPT` subclass) and export it. Read the `reasoning` fields. |
| Errors | `_is_non_retryable` (1425-1447) treats any 4xx but 408/429 as permanent; `Runner/Persistence.py:489` `is_transient` is a second classifier; `_with_retry` (1450-1525). | One classifier for "fails for good" (the **Fallback** table), reusing these. |
| Empty answers | `AgentRun.py`: outcome starts `done` (2229); text-only finals at 2285-2299, 2535, 2635; empty `Final_Answer` at 2661; `respond` without `answer` at 2328 all end `done` with an empty answer. The implicit-final branch (2836-2843) already catches it. | Goal 3 at each of these. The team layer's `NO_ANSWER_START` (`cli/team.py:67-70`) shows the wording already used for a step with no answer. |
| Native binding | `AgentRun.py:1501` `bind_tools_natively=False`; the CLI never sets it (`cli/app.py:540`, `cli/team.py:644`, `668`). | The runner reads the model's preference (goal 1). |
| Names | `cli/models.py`: `provider_for` (41-48) returns None for other names; `choose_model` (91-119) never passes `base_url`; `parse_team_models` (~198) rejects names without a provider. `tests/test_cli_models.py:27` asserts `ollama` has no provider. | Other names are local (see **Names**); that test changes on purpose. |
| Credit errors | `cli/app.py:112-124` `_NO_CREDIT` matches "credit balance is too low", "insufficient_quota", "exceeded your current quota"; the turn error handler (1044-1049) keeps the stopped turn. | The fallback hook for the chat; reuse the matcher. |
| Team failures | `cli/team.py:645-653` `_HelperRunner.Initialize` re-raises a model error as `RuntimeError`; the Supervisor marks the step failed and skips dependants. `format_result` (714-745) writes step states. | The fallback hook for helpers; a new step state for "done on the fallback". |
| Settings | `cli/settings.py`: `Settings` (21-34), `load_settings` (~289); project rules for `mode`, `tokenLimit`, `addDirs`. Keys live in the OS key store (`cli/keystore.py`, providers claude and openai). | `localServer`, `fallbackModel` (user file only), and `localContext` if it ships. Local models need no stored key. |
| App window | `cli/webui.py` `state()` (1046-1072) builds the model list from the current model, team models and `MODEL_SUGGESTIONS` (67); the menu posts `/model <name>` (`resources/webui/app.js:965-980`); Settings form `settings_view` (1255) / `save_settings` (1285); notices via `addNotice` (`app.js:445`); team lines via `ui.py:389` `team_done` and the Details "Team work" list. | Local models in the list and the datalist, marked; the new settings in the form; the fallback notice and chip; the fallback step mark in the terminal, the app's team lines and Details. |
| Docs | `docs/concepts/models.md:10`, `278-298` show a hand-written `Ollama(BaseChatModel)` on Ollama's own API; `docs/getting-started.md:62` and `docs/guides/yaml-config.md:72`, `159` mention local servers. | Point them at `Local`. |

## Calls made by the writer (not discussed with the requester)

- Any name that is not `claude*`, `gpt*` or `o<digit>*` is local; no prefix.
- `qwen3-agent` goes in the requester's own settings, never in code; `Local()` without a name uses the server's
  only model.
- `localServer` and `fallbackModel` are user-file only; `fallbackModel` must be local.
- On a local chat model, only automatic cloud calls are forbidden; explicit cloud helpers and voice stay allowed.
- The window is kept, not just watched; the request is refused only when even the instructions do not fit.
- An empty answer after real tool work stays `done` with a note; only a run that did nothing is not `done`.
- The running turn continues on the fallback (never restarts); a team step reruns told what the failed attempt
  did.
- At startup, today's offer of the other paid provider comes before the local fallback.
- The fallback lives in the runner, not a wrapper model.

## What is left to the builder

- Class and function names beyond `Local`, parameter names, the event's shape, and where the failure
  classification lives.
- The check's tool and prompt.
- How tokens are estimated, and the exact shares of the window.
- Styling of the `local` and `fallback` marks in the app window.

## Testing

- A fake OpenAI-compatible server (or stubbed client) covering: model list, a tool call, a text-only answer, a
  missing model, a refused connection, a slow first answer, `prompt_tokens` near the window, Ollama's `/api/ps`
  and `/api/show`.
- The window budget: long `AGENTX.md` and log shortened; old tool results stubbed within a turn; a summary
  between two steps; the summary's own input capped; the refusal when instructions alone do not fit; a long
  Claude history summarised before the fallback sees it.
- A turn interrupted by a permanent failure continues on the fallback without repeating an approved tool call.
- Fallback: each row of the table, both directions of "falls back / does not", stickiness, the marks in the chat
  and in a team job, local never falling back to paid.
- Settings: a project file setting `localServer` or `fallbackModel` is ignored with a warning.
- Startup with no local server is not slower (the lookup is in the background).
- An empty final answer is not `done`, for every model.
- By hand on the requester's laptop: agentx on `qwen3-agent` does a small edit task; a team job with an invalid
  Claude key finishes on `qwen3-agent` and is marked.
