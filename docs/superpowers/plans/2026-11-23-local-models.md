# Local Models Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run agentx_dev and the `agentx` app on local OpenAI-compatible model servers (Ollama, LM Studio, llama.cpp), and fall back to a local model automatically, marked, when Claude or GPT fails for good.

**Architecture:** A `Local` chat model (a `GPT` subclass on chat completions) carries `is_local`, `prefers_native_tools` and `context_window`. The runner reads those: it binds tools natively for such a model, keeps each request inside the window, and, given `fallback_model=`, switches models mid-run on a failure classified as permanent, continuing the same run. agentx wires names, settings, the check, discovery, the chat and team fallbacks and the app window onto that.

**Tech Stack:** Python 3.10+, `openai` client (already a dependency), `urllib.request` for Ollama's read-only `/api/ps` and `/api/show`, pytest, the app window's vanilla JS.

**Spec:** `docs/superpowers/specs/2026-11-23-local-models-design.md`

**Where the plan goes past the spec (rulings):**
- A team step whose helper fails for good does not run again: its runner switches inside the run (Task 6) and continues with its own history, which already holds what it did. That meets the spec's "not blind, never repeat approved work" more directly than a rerun with a summary.
- "Other helpers on the same provider switch too" happens lazily: each helper switches on its own first failed call (a rejected key or no credit fails at once, so the cost is one quick failed request per helper).

## Global Constraints

- Python 3.10+; no new dependencies.
- Windows, macOS and Linux. Tests never need a running model server: use a stub client or a `http.server` thread on `127.0.0.1:0`.
- A `claude*`, `gpt*` or `o<digit>*` name means exactly what it means today; every other name is local, with no prefix.
- Default local address: `http://localhost:11434/v1`.
- A `Local` model never reads `OPENAI_API_KEY`.
- Fallback only from a non-local model to a local one in agentx; a local model never falls back to anything.
- A project's `.agentx/settings.json` may not set `localServer`, `fallbackModel` or `localContext` (ignored with the usual one-line warning).
- `fallbackModel` must be a local name: a paid name is refused when saved and ignored with a warning when read.
- Work done on the fallback is always marked; the exact texts are in the tasks below and come from the spec.
- No code, default or test fixture names `qwen3-agent` except the manual check in Task 15.
- Commit messages: plain conventional commits; **never** a `Co-Authored-By` or any AI attribution line. Stage files by explicit path; never stage `.superpowers/`.
- Use the Edit tool (not shell heredocs) for any text with backslashes or `\n`.

## Review Focus

1. Ollama stopped mid-conversation while the chat model is local: the turn fails with "the local server at localhost:11434 is not answering" and nothing is sent to a paid model. Test in Task 12.
2. Names with a tag or a colon (`qwen3:8b`, `llama3.1:8b-instruct-q4_K_M`) work as chat models, in `teamModels`, in `role@model` helper names and in `/model`. Tests in Tasks 9 and 13.
3. `localServer` written without `/v1` or with a trailing slash (`http://localhost:11434`, `http://localhost:11434/v1/`) reaches the same server. Test in Task 2.
4. A local server that is up but slow to answer (a model loading) never delays agentx's start; the model menu fills in when the answer arrives. Test in Task 11.
5. A Claude history holding tool calls and tool results, handed to a local model on fallback, is valid chat-completions input (no tool messages without a matching call, no calls to unbound functions). Test in Task 6.

---

### Task 1: GPT reads the thinking local servers send

**Files:**
- Modify: `agentx_dev/ChatModel.py` (`GPT.call_with_tools`, ~1868-1963; the chat-completions branch of `GPT.Initialize`/`extract_content`, ~2038)
- Test: `tests/test_reasoning_shown.py`

**Interfaces:**
- Produces: `call_with_tools(...)` returns `"reasoning": str` when a chat-completions message carries a non-empty `reasoning` (Ollama) or `reasoning_content` (LM Studio, vLLM) attribute or dict key; absent otherwise. Content is never altered.

- [ ] **Step 1: Write the failing tests** — with a stubbed `client.chat.completions.create` returning a message whose `content=""`, `tool_calls=None`, extra field `reasoning="thinking…"`: `test_chat_completions_reasoning_field_is_returned` asserts `result["reasoning"] == "thinking…"` and the text part is `""`; `test_reasoning_content_field_is_returned` the same for `reasoning_content`; `test_no_reasoning_field_no_key` asserts `"reasoning" not in result` for a plain OpenAI message.
- [ ] **Step 2: Run** `pytest tests/test_reasoning_shown.py -v` — Expected: the three new tests FAIL.
- [ ] **Step 3: Implement** `_chat_reasoning(message) -> str` in `ChatModel.py` (reads attribute or `model_extra`/dict key, `reasoning` first) and use it in the chat-completions path of `call_with_tools`.
- [ ] **Step 4: Run** `pytest tests/test_reasoning_shown.py tests/test_responses_fallback.py -v` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(models): chat completions return the thinking that local servers send`

### Task 2: The `Local` model

**Files:**
- Create: `agentx_dev/LocalModel.py`
- Modify: `agentx_dev/__init__.py` (export `Local`, `LocalModelError`, `LocalCheck`)
- Test: `tests/test_local_model.py` (a fake server: `http.server.ThreadingHTTPServer` on `127.0.0.1:0` serving `/v1/models`, `/v1/chat/completions`, `/api/ps`, `/api/show`)

**Interfaces:**
- Produces:
  - `DEFAULT_LOCAL_URL = "http://localhost:11434/v1"`
  - `normalise_base_url(url: str) -> str` — strips trailing `/`, appends `/v1` when the path is empty.
  - `class LocalModelError(Exception)`
  - `@dataclass(frozen=True) class LocalCheck: ok: bool; reason: str; context_window: Optional[int]`
  - `class Local(GPT)`: `__init__(self, model: Optional[str] = None, base_url: str = DEFAULT_LOCAL_URL, api_key: Optional[str] = None, context_window: Optional[int] = None, timeout: float = 300.0, **kw)`; class attributes `is_local = True`, `prefers_native_tools = True`; instance `context_window` (given, else `None` until `check()`); always `use_responses_api=False`; `api_key` defaults to the placeholder `"local"` (never the environment).
  - `Local.models(base_url: str = DEFAULT_LOCAL_URL, timeout: float = 5.0) -> List[str]` (static; raises `LocalModelError` with a short reason on refusal/timeout/bad reply).
  - `Local.check(self, timeout: float = 120.0) -> LocalCheck` — server answers, model listed, one tiny request with one tool comes back as a tool call; on Ollama, sets and returns the window (`/api/ps` `context_length` for a loaded model, else `num_ctx` from `/api/show` `parameters`, else 8192).
  - `Local.server_window(self, timeout: float = 5.0) -> Optional[int]` (the Ollama lookup alone; `None` for other servers).

- [ ] **Step 1: Write the failing tests**
  - `test_local_never_reads_openai_key(monkeypatch)`: with `OPENAI_API_KEY=sk-real`, `Local("m", base_url=fake).client.api_key == "local"`.
  - `test_local_uses_chat_completions_only`: `Local("m").use_responses_api is False`.
  - `test_base_url_is_normalised`: `normalise_base_url("http://localhost:11434") == normalise_base_url("http://localhost:11434/v1/") == "http://localhost:11434/v1"`.
  - `test_no_name_uses_the_only_model` (fake lists `["only:1b"]`) → `Local(base_url=fake).model == "only:1b"`; `test_no_name_several_models_names_them` → `LocalModelError` whose text contains both names; `test_no_name_no_models` → `LocalModelError("… lists no models")`.
  - `test_models_lists_names`; `test_models_refused_connection_says_not_running` (closed port) → message contains `"is not answering"` and the host:port.
  - `test_check_ok_reads_loaded_window` (`/api/ps` reports 8192) → `LocalCheck(True, "", 8192)` and `model.context_window == 8192`; `test_check_reads_num_ctx_when_not_loaded` (`/api/show` parameters `num_ctx 16384`) → 16384.
  - `test_check_missing_model` → `ok False`, reason contains `"no model named"` and the listed names.
  - `test_check_text_instead_of_tool` (fake answers text) → reason contains `"answered in text instead of calling the tool"`.
- [ ] **Step 2: Run** `pytest tests/test_local_model.py -v` — Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `agentx_dev/LocalModel.py` per the Interfaces block; HTTP for `/api/*` via `urllib.request` with the given timeout; the check's tool is a one-argument `ping(text)` function and the prompt asks the model to call it.
- [ ] **Step 4: Run** `pytest tests/test_local_model.py -v` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(models): Local, a chat model for Ollama, LM Studio and other OpenAI-compatible servers`

### Task 3: The runner binds tools natively when the model prefers it

**Files:**
- Modify: `agentx_dev/Runner/AgentRun.py` (`__init__` ~1501 and the function-calling auto-detect/exclusivity at ~1777-1800)
- Test: `tests/test_local_runner.py`

**Interfaces:**
- Consumes: `prefers_native_tools` (Task 2).
- Produces: `AgentRunner(bind_tools_natively: Optional[bool] = None)`. `None` means `bool(getattr(model, "prefers_native_tools", False))`; `True`/`False` as given. When it resolves to `True` from the model, function-calling auto-detection is skipped (`use_function_calling` stays off); an explicit `use_function_calling=True` with a preferring model resolves native binding to `False` instead of raising.
- `system_addendum` still reaches the model in native mode (it already does at ~2144; the test pins it).

- [ ] **Step 1: Write the failing tests** using `conftest.MockModel` subclasses: `test_preferring_model_binds_natively` (`runner.bind_tools_natively is True`, `use_function_calling` falsy); `test_explicit_false_wins`; `test_other_models_unchanged` (`MockModel` → `False`); `test_addendum_reaches_native_system_prompt` (the first message sent contains the addendum text); `test_explicit_function_calling_beats_preference`.
- [ ] **Step 2: Run** `pytest tests/test_local_runner.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** the resolution in `__init__`.
- [ ] **Step 4: Run** `pytest tests/test_local_runner.py tests/test_runner_loop.py -v` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(runner): bind tools natively for models that prefer it`

### Task 4: An empty final answer is never passed off as an answer

**Files:**
- Modify: `agentx_dev/Runner/AgentRun.py` (final-answer sites ~2285-2299, 2328, 2535, 2635, 2661; outcome starts at ~2229), `agentx_dev/Runner/AsyncAgentRun.py` (its equivalent sites)
- Test: `tests/test_empty_final_answer.py`

**Interfaces:**
- Produces: outcome `"no_answer"` and content `NO_ANSWER_TEXT = "The model gave no answer."` when the run made no tool calls and the final answer is empty (whitespace only, no structured output). When it made tool calls: outcome unchanged (`"done"`), content `"The model gave no final answer. It called: <names in order, de-duplicated> (files changed: <paths>)"`, the files part only when `write_file`/`edit_file` calls succeeded (reuse the refusal-skipping rule of `cli/team.py` `_changed_files`; move that helper to `agentx_dev/Runner/AgentRun.py` or a small shared module and import it back into `team.py`).

- [ ] **Step 1: Write the failing tests** (sync and async, `MockModel` scripted): `test_no_tools_empty_answer_is_no_answer` (`outcome == "no_answer"`, `content == "The model gave no answer."`); `test_tool_work_then_empty_answer_is_done_with_note` (`outcome == "done"`, content starts with `"The model gave no final answer. It called: read_path, write_file"` and contains `"files changed: done.txt"`); `test_native_respond_without_answer` (native mode, `respond({})` after no tools → `no_answer`); `test_empty_final_answer_action` (ReAct `Final_Answer` with `""`); `test_normal_answer_untouched`.
- [ ] **Step 2: Run** `pytest tests/test_empty_final_answer.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** one helper `_settle_empty_final(final_answer, output, tool_calls) -> Tuple[str, Optional[str]]` (content, outcome override) called at every listed site.
- [ ] **Step 4: Run** `pytest tests/test_empty_final_answer.py tests/test_runner_loop.py tests/test_empty_step_retry.py tests/test_supervisor_outcomes.py tests/test_cli_team.py -v` — Expected: PASS.
- [ ] **Step 5: Commit** `fix(runner): an empty final answer is reported, not passed off as done`

### Task 5: Classify failures that waiting will not fix

**Files:**
- Create: `agentx_dev/Runner/Fallback.py`
- Modify: `agentx_dev/cli/app.py:112-124` (`_out_of_credit` uses `NO_CREDIT_WORDS` from the new module)
- Test: `tests/test_fallback_classify.py`

**Interfaces:**
- Produces: `NO_CREDIT_WORDS = ("credit balance is too low", "insufficient_quota", "exceeded your current quota")`; reasons `OUT_OF_CREDIT = "out of credit"`, `KEY_REJECTED = "key rejected"`, `MODEL_NOT_FOUND = "model not found"`, `UNREACHABLE = "unreachable"`; `fails_for_good(error: BaseException) -> Optional[str]`.
- Rules (spec's table): credit words anywhere in `str(error)` → `OUT_OF_CREDIT` (whatever the status); status 401/403 → `KEY_REJECTED`; 404 → `MODEL_NOT_FOUND`; connection errors, timeouts, 5xx, and class names matching `Connection|Timeout|ServiceUnavailable|InternalServer|Overloaded` → `UNREACHABLE` (they reach the runner only after the client's retries are spent); 429 without credit words, other 4xx, `ModelDeclined`, `CostBudgetExceeded`, `RetryBudgetExceeded`, `KeyboardInterrupt` and anything else → `None`.

- [ ] **Step 1: Write the failing tests**: a parametrised `test_fails_for_good` over fake exceptions carrying `status_code` and a message, one row per rule above, including `(400, "Your credit balance is too low") → "out of credit"`, `(429, "insufficient_quota") → "out of credit"`, `(429, "rate limit") → None`, `(400, "bad request") → None`, `ConnectionError() → "unreachable"`; `test_app_out_of_credit_still_matches` for `cli.app._out_of_credit`.
- [ ] **Step 2: Run** `pytest tests/test_fallback_classify.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** `fails_for_good`.
- [ ] **Step 4: Run** `pytest tests/test_fallback_classify.py tests/test_cli_app*.py -q` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(runner): tell failures that waiting will not fix from the rest`

### Task 6: The runner falls back mid-run

**Files:**
- Modify: `agentx_dev/Runner/AgentRun.py` (`__init__`, `_iter_run_core`'s `_model_call` ~2233 and the loop around it), `agentx_dev/Agents/Agent.py:491` (`AgentCompletion`)
- Test: `tests/test_runner_fallback.py`

**Interfaces:**
- Consumes: `fails_for_good` (Task 5), `prefers_native_tools`/`is_local` (Task 2), the native resolution (Task 3).
- Produces:
  - `AgentRunner(fallback_model: Optional[BaseChatModel] = None)`; `runner.fallback_used: Optional[Dict[str, str]]` (`{"from": label, "to": label, "reason": reason}`), sticky across runs; `runner.reset_fallback() -> None` restores the primary model and its binding mode.
  - `AgentCompletion.fallback: Optional[Dict[str, str]] = None` (same dict, set on every completion produced while switched).
  - Event `{"type": "fallback", "from": str, "to": str, "reason": str}` yielded once, at the switch.
  - `class FallbackFailed(RuntimeError)` (in `Runner/Fallback.py`), raised when the fallback's call fails too; its text names both: `"<from> failed (<reason>); the fallback <to> failed too: <error>"`.
- Behaviour: when a model call raises and `fails_for_good(e)` is not `None`, `fallback_model` is set, not yet used, and the current model is not `is_local`: log a warning, switch `self.model`, re-resolve native binding for the new model, rebuild `working_history[0]` (the system message) for the new mode, convert the history so it is valid for the new mode (assistant tool calls to functions the new mode does not bind, and their results, become plain text messages: `"[earlier step] called <name> with <args>"` / `"[earlier result] <text>"`), yield the event, and retry the same call on the fallback. Approved tool calls already in the history are never run again. In every other case the error propagates as today.

- [ ] **Step 1: Write the failing tests** with a primary `MockModel` whose call raises a fake 401 and a fallback `MockModel` with `prefers_native_tools = True`: `test_switches_and_finishes_on_fallback` (`completion.content` from the fallback, `completion.fallback == {"from": ..., "to": ..., "reason": "key rejected"}`, a `fallback` event in `runner.stream(...)` events); `test_tool_call_before_failure_is_not_repeated` (primary makes one tool call, then fails; the tool's counter is 1 at the end); `test_history_valid_for_native_mode` (after the switch, no message in what the fallback receives has role `tool` without a preceding assistant `tool_calls` entry for the same id, and no `tool_calls` name outside the fallback's bound tools) — Review Focus 5; `test_sticky_next_run_starts_on_fallback`; `test_reset_fallback_restores_primary`; `test_transient_error_does_not_switch` (429 rate limit propagates); `test_local_model_never_falls_back` (primary has `is_local = True`, error propagates); `test_fallback_failing_raises_fallback_failed` (text contains both reasons).
- [ ] **Step 2: Run** `pytest tests/test_runner_fallback.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** per the Interfaces block. The retry re-enters the current loop iteration (a private exception caught at the iteration's top is fine); the mode branch must be re-read after the switch.
- [ ] **Step 4: Run** `pytest tests/test_runner_fallback.py tests/test_runner_loop.py tests/test_local_runner.py -v` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(runner): fallback_model takes over a run when the model fails for good`

### Task 7: Supervisor passes the fallback to its helpers

**Files:**
- Modify: `agentx_dev/Supervisor.py:68` (`Supervisor.__init__`), `agentx_dev/_supervisor/core.py` (`_spawn_spec` and where registered agents are run), `agentx_dev/SubAgents.py` (where a spawned helper's runner is built)
- Test: `tests/test_supervisor_fallback.py`

**Interfaces:**
- Consumes: `AgentRunner(fallback_model=...)` (Task 6).
- Produces: `Supervisor(..., fallback_model: Optional[BaseChatModel] = None)`; every runner it spawns gets it; a registered runner whose `fallback_model` is `None` gets it set before its first step; one that has its own keeps it. The supervisor's own planning model is not switched (out of scope).

- [ ] **Step 1: Write the failing tests** with `tests/subagent_helpers.py` (`router`, `plan_json`, `step`, `new_agent_step`): `test_registered_runner_gets_fallback`; `test_own_fallback_kept`; `test_spawned_helper_gets_fallback`; `test_step_result_carries_fallback_mark` (a helper whose primary raises 401 finishes on the fallback; the step's completion has `.fallback`).
- [ ] **Step 2: Run** `pytest tests/test_supervisor_fallback.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest tests/test_supervisor_fallback.py tests/test_supervisor_core.py tests/test_supervisor_outcomes.py -v` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(supervisor): pass fallback_model to the helpers it builds`

### Task 8: The runner keeps each request inside the model's window

**Files:**
- Modify: `agentx_dev/Runner/AgentRun.py` (`__init__`, `_model_view` ~2018, the model-call sites), `agentx_dev/Runner/Trim.py` (a budget-driven `TrimPolicy` if needed)
- Test: `tests/test_runner_context_window.py`

**Interfaces:**
- Consumes: `context_window` on the model (Task 2); `trim_tool_results`/`TrimPolicy` (`Runner/Trim.py`); `compact_history` (`Runner/Persistence.py:458`).
- Produces:
  - `AgentRunner(context_window: Optional[int] = None)`; effective window = this, else `getattr(model, "context_window", None)`; `None` means no budget (today's behaviour, unchanged).
  - `CHARS_PER_TOKEN = 4`; `REQUEST_SHARE = 0.85` (a request may use 85% of the window; the rest is for the answer); when a reply reports `usage.prompt_tokens`, the runner keeps the observed characters-per-token for that model and uses it instead of 4.
  - Order when the request is over budget: (1) stub old tool results, newest kept whole; (2) `compact_history` in place, its summarise call's input capped to half the window; (3) raise `ContextWindowTooSmall(window: int, needed: int)` (new, in `Runner/Fallback.py` or `AgentRun.py`, exported) when the system message plus the newest user message alone exceed the budget.
  - Event `{"type": "context_trimmed", "window": int, "how": "results" | "summary"}` the first time (1) or (2) acts in a run.
  - The window follows a fallback switch (Task 6): after switching, the budget is the fallback's window, applied before the retried call.

- [ ] **Step 1: Write the failing tests**: `test_no_window_no_change` (history sent unchanged); `test_old_tool_results_stubbed_to_fit` (window 2,000 tokens, five 3,000-char results: the request sent is ≤ 0.85×2,000×4 characters and the newest result is whole); `test_summary_when_stubs_not_enough` (event `how == "summary"`; the summarise call's input ≤ 4,000 characters for a 2,000-token window); `test_too_small_raises` (system prompt alone 10,000 chars, window 1,000 → `ContextWindowTooSmall` with `window == 1000`); `test_observed_ratio_used`; `test_budget_follows_fallback` (primary unlimited, fallback window 2,000: the retried request fits).
- [ ] **Step 2: Run** `pytest tests/test_runner_context_window.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest tests/test_runner_context_window.py tests/test_runner_fallback.py tests/test_persistence_compaction.py tests/test_runner_loop.py -v` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(runner): keep each request inside the model's context window`

### Task 9: agentx knows local names

**Files:**
- Modify: `agentx_dev/cli/models.py` (`provider_for` 41-48, `choose_model` 91-119, `model_label` 122-135, `parse_team_models` ~198, `usable_team_models` ~241, `build_team_models` ~278, `_acceptable_name`), wherever `provider_name` lives (used by `cli/app.py` messages)
- Test: `tests/test_cli_models.py`, `tests/test_cli_helper_models.py`

**Interfaces:**
- Consumes: `Local`, `DEFAULT_LOCAL_URL` (Task 2).
- Produces:
  - `LOCAL = "local"`; `provider_for(name) -> Optional[str]` returns `"local"` for every non-empty name that is not Claude or GPT (the existing test row `("ollama", None)` becomes `("ollama", "local")`); `is_local_name(name: str) -> bool`.
  - `choose_model(name, env, *, local_server: str = DEFAULT_LOCAL_URL, local_context: Optional[int] = None)` builds `Local(name, base_url=local_server, context_window=local_context, timeout=MODEL_TIMEOUT_SEC)` for a local name; no key needed. Without a name, behaviour is unchanged (keys decide).
  - `LOCAL_TYPO_HINT = "Claude names start with claude, GPT names with gpt."`, appended to a failed check's reason when the local name starts with `cl` or `gp` (used in Task 11).
  - `parse_team_models` accepts local names; `usable_team_models` needs no key for them; `model_label(Local)` is the model name; `provider_name("qwen3-agent") == "the local server"`.

- [ ] **Step 1: Write the failing tests**: `test_other_names_are_local` (`qwen3-agent`, `qwen3:8b`, `llama3.1:8b-instruct-q4_K_M`, `ollama`); `test_claude_gpt_unchanged`; `test_choose_model_local_needs_no_key` (empty env → a `Local` with `base_url` normalised, `context_window` passed); `test_team_models_accept_local_names` (`parse_team_models(["qwen3:8b=\"small edits\""])` keeps name and note, no problem); `test_usable_local_without_key`; `test_colon_names_acceptable` (Review Focus 2).
- [ ] **Step 2: Run** `pytest tests/test_cli_models.py tests/test_cli_helper_models.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same — Expected: PASS.
- [ ] **Step 5: Commit** `feat(agentx): any name that is not Claude or GPT is a local model`

### Task 10: agentx settings for local models

**Files:**
- Modify: `agentx_dev/cli/settings.py` (`Settings` 21-34, `load_settings` ~289, a user-file writer next to `_write_project_key`)
- Test: `tests/test_cli_settings.py`

**Interfaces:**
- Produces:
  - `Settings.local_server: str = DEFAULT_LOCAL_URL` (normalised), `Settings.fallback_model: Optional[str] = None` (`None` = never set; `""` = set to off), `Settings.local_context: Optional[int] = None` — appended after the existing fields (a test pins field order).
  - Keys `localServer`, `fallbackModel`, `localContext`, read from the user file only; in a project file each is ignored with `"<key> in <project file> is ignored: only your own settings file can set it."`
  - `fallbackModel` naming Claude or GPT: ignored with `"fallbackModel must be a local model; <name> is not."`; `localContext` not a positive int: ignored with a warning.
  - `localServer` whose host is not `localhost`, `127.0.0.1` or `::1`: kept, and `Settings.problems` gets `"Conversations on local models go to <host>."`
  - `write_user_setting(home: Path, key: str, value: Any) -> None` (same safe-write rules as the project writer: temp file, no symlink following, 0o600).

- [ ] **Step 1: Write the failing tests**: one per rule above, plus `test_fallback_off_is_empty_string` and `test_user_writer_refuses_symlink`.
- [ ] **Step 2: Run** `pytest tests/test_cli_settings.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same — Expected: PASS.
- [ ] **Step 5: Commit** `feat(agentx): localServer, fallbackModel and localContext settings`

### Task 11: agentx switches to, finds and fits local models

**Files:**
- Modify: `agentx_dev/cli/app_models.py` (`switch_model` 37), `agentx_dev/cli/app.py` (`main()` model building, `_make_runner` ~536, `send` ~981, startup), `agentx_dev/cli/context.py` (`build_addendum` 176), `agentx_dev/cli/app_usage.py` (`_maybe_compact` 145), `agentx_dev/cli/compact.py` (`compact` 214, `transcript` 157), `agentx_dev/cli/commands.py` (`/model` with no name)
- Test: `tests/test_cli_local_app.py`

**Interfaces:**
- Consumes: Tasks 2, 8, 9, 10.
- Produces:
  - On choosing a local model (`/model`, the menu, `--model`, a settings `model`): `Local.check()` runs with the progress line `"Loading <name> on the local server"`; on `ok` the switch happens and `runner` gets the checked window; otherwise the current model stays and the error line is `"Could not use <name>: <reason>"` (+ `LOCAL_TYPO_HINT` per Task 9). At startup a failed check falls back to today's no-model path with that line.
  - `app.local_models: List[str]`, filled by a daemon thread started at startup calling `Local.models(settings.local_server, timeout=0.5)`; startup never joins it. `/model` with no name lists them after the current model as `<name> (local)`.
  - `build_addendum(..., max_context_chars: int = MAX_CONTEXT_CHARS, max_log_chars: int = MAX_LOG_CHARS)`; for a local model with window W tokens the app gives the instructions a third of the window: `share = W * 4 // 3` characters, `max_log_chars = min(MAX_LOG_CHARS, share // 4)`, `max_context_chars = min(MAX_CONTEXT_CHARS, share - max_log_chars)`. The addendum says `"(shortened to fit <name>'s <W>-token window)"` when it cut.
  - `_maybe_compact` threshold for a local model: `int(W * 4 * 0.6)` characters; `compact(..., max_input_chars=int(W * 4 * 0.5))` (new keyword on `compact`/`transcript`, default `MAX_INPUT_CHARS`).
  - `send` catches `ContextWindowTooSmall` and shows: `"<name>'s <W>-token window is too small for this conversation. A variant with a larger window fixes it: a Modelfile with FROM <name> and PARAMETER num_ctx 16384, then ollama create <name>-16k -f Modelfile."`
  - The first `context_trimmed` event in a conversation shows the notice `"<name> is working with less than the whole conversation: older tool results were shortened to fit its <W>-token window."` (or `"…the earlier conversation was summarised…"` for `"summary"`).

- [ ] **Step 1: Write the failing tests** with a stubbed `Local.check`/`Local.models`: `test_switch_to_local_runs_check_and_switches`; `test_failed_check_keeps_model_and_says_why`; `test_typo_gets_hint` (`cluade-opus-5-5`); `test_startup_does_not_wait_for_discovery` (models stub sleeps 2 s; app construction returns in < 1 s) — Review Focus 4; `test_model_lists_local_models`; `test_addendum_shortened_for_window`; `test_compaction_threshold_follows_window`; `test_too_small_message`; `test_trimmed_notice_once_per_conversation`.
- [ ] **Step 2: Run** `pytest tests/test_cli_local_app.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest tests/test_cli_local_app.py tests/test_cli_compact.py tests/test_cli_context.py tests/test_cli_commands.py -v` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(agentx): choose, check and fit local models`

### Task 12: agentx falls back in the chat

**Files:**
- Modify: `agentx_dev/cli/app.py` (`_make_runner`, `send`'s event loop and error handler, startup's no-key path), `agentx_dev/cli/app_models.py`, `agentx_dev/cli/commands.py` (new `/fallback`), `agentx_dev/cli/memory.py` (log entry), session saving
- Test: `tests/test_cli_fallback.py`

**Interfaces:**
- Consumes: Tasks 6, 9, 10, 11.
- Produces:
  - `_make_runner` passes `fallback_model=choose_model(settings.fallback_model, ...)` when `settings.fallback_model` is set and the chat model is not local; the fallback model is checked lazily (Task 6 switches only when needed; a failed fallback call surfaces as `FallbackFailed`).
  - On the `fallback` event: `app.model` becomes the fallback model, `app.fallback_from = {"model": <from>, "reason": <reason>}`, and the notice `"<From> failed: <reason>. This conversation continues on <to> (local). Choose a model to try <From> again."` The next turns' runners are built on the fallback. Choosing a model (`/model`, menu) clears `fallback_from`.
  - Startup without a key: today's `other_provider` offer first; when none or declined and `fallback_model` is set, start on it with `"No key for <provider>: starting on <fallback> (local)."`
  - `/fallback` (no arg: shows it), `/fallback <local name>` (runs the check, saves with `write_user_setting`), `/fallback off` (saves `""`). Summary for the `/` list: `"Show or set the local model used when Claude or GPT fails: /fallback [name|off]"`.
  - First-run offer: when discovery (Task 11) finds models and `settings.fallback_model is None`, one notice: `"<Server> is running with <names>. Use one as the fallback when Claude or GPT fails? /fallback <first name> sets it; /fallback off stops this question."` with `actions` `[{"label": "Use <first>", "send": "/fallback <first>"}, {"label": "No", "send": "/fallback off"}]` on the notice event (the app window draws them in Task 14; the terminal shows the text).
  - The work-log entry and the saved session for a turn that ran on the fallback record `"(on <to>, fallback from <from>: <reason>)"`.

- [ ] **Step 1: Write the failing tests** (fake models; primary raises a fake 402/credit error mid-turn after one approved tool call): `test_turn_continues_on_fallback_without_repeating_tool`; `test_notice_text`; `test_sticky_until_model_chosen`; `test_local_chat_never_falls_back` (chat model local, server refused → error `"the local server at localhost:11434 is not answering"`, no paid call made) — Review Focus 1; `test_startup_no_key_uses_fallback_after_offer`; `test_fallback_command_rejects_paid_name`; `test_fallback_off_saves_empty`; `test_offer_shown_once`; `test_log_entry_marks_fallback`.
- [ ] **Step 2: Run** `pytest tests/test_cli_fallback.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest tests/test_cli_fallback.py tests/test_cli_commands.py tests/test_cli_memory*.py -v` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(agentx): fall back to the local model when Claude or GPT fails for good, and say so`

### Task 13: Team jobs with local helpers and the fallback

**Files:**
- Modify: `agentx_dev/cli/team.py` (`build_helpers` 675, helper runner 644/668, `_HelperRunner.Initialize` 645-653, `format_result` 714-745), `agentx_dev/cli/app.py` (`_start_team_job` ~1459, the job approval text), `agentx_dev/cli/ui.py` (`team_done` 389), `agentx_dev/cli/app_team.py` (result events 102-115)
- Test: `tests/test_cli_team.py`, `tests/test_cli_team_app.py`

**Interfaces:**
- Consumes: Tasks 6, 9, 12.
- Produces:
  - Helpers on local models are named `role@<name>` (colons kept: `coder@qwen3:8b`) — Review Focus 2.
  - Every helper runner whose model is not local gets `fallback_model` (the app's fallback); a helper finishing on it carries `completion.fallback`.
  - `format_result`: a step that ran on the fallback reads `done on <to> (fallback: <from> <reason>)`; the header adds `"; <N> step(s) ran on the fallback model; check their work"`. Partial and fallback combine as `partial on <to> (…)`.
  - The `result` event gains `"fallback": "<to>"`; `ui.team_done` prints the mark `↺` and `"on <to> (fallback)"`.
  - A team job started while the chat model is local and `teamModels` lists a Claude or GPT name: the approval adds `"Helpers on <names> will see this project's files."`

- [ ] **Step 1: Write the failing tests**: `test_local_helper_names_keep_colons`; `test_helper_gets_fallback_unless_local`; `test_step_on_fallback_line_and_header` (exact strings above); `test_partial_on_fallback`; `test_team_done_fallback_mark`; `test_cloud_helpers_warned_on_local_chat`.
- [ ] **Step 2: Run** `pytest tests/test_cli_team.py tests/test_cli_team_app.py -v` — Expected: the new tests FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest tests/test_cli_team*.py tests/test_cli_ui.py -v` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(agentx): local helpers in team jobs, and steps that ran on the fallback are marked`

### Task 14: The app window

**Files:**
- Modify: `agentx_dev/cli/webui.py` (`state()` 1046-1072, `settings_view` 1255, `save_settings` 1285, notice events), `agentx_dev/resources/webui/app.js` (model menu 965-980, settings form ~1160, `addNotice` 445, Details "Team work" ~1094), `agentx_dev/resources/webui/style.css`
- Test: `tests/test_webui.py`

**Interfaces:**
- Consumes: `app.local_models` (Task 11), `app.fallback_from` (Task 12), notice `actions` (Task 12), the `result` event's `fallback` (Task 13), settings keys (Task 10).
- Produces:
  - `/api/state` adds `"local_models": [...]` and `"fallback_from": {"model", "reason"} | null`; `models` includes the local names (de-duplicated).
  - The menu labels a local name `<name> · local`; the model chip shows `<name> · fallback` while `fallback_from` is set.
  - Settings form: fields `localServer`, `fallbackModel` (with the local names as suggestions) and `localContext`, saved to the user file only; a paid `fallbackModel` is refused with the Task 10 message.
  - A notice with `actions` draws one button per action that posts its `send` text to `/api/send`.
  - Details "Team work" shows `↺ on <to> (fallback)` for such steps.

- [ ] **Step 1: Write the failing tests** (Python side): `test_state_lists_local_models`; `test_state_fallback_from`; `test_settings_view_has_local_keys`; `test_save_settings_writes_user_file_only`; `test_save_settings_refuses_paid_fallback`; `test_notice_event_carries_actions`.
- [ ] **Step 2: Run** `pytest tests/test_webui.py -v` — Expected: the new tests FAIL.
- [ ] **Step 3: Implement** the Python side, then the JS/CSS; run `node --check agentx_dev/resources/webui/app.js`.
- [ ] **Step 4: Run** `pytest tests/test_webui.py -v` — Expected: PASS. Then open the app window with a stubbed local list and check by eye: the menu marks, the chip, the settings fields, the offer buttons.
- [ ] **Step 5: Commit** `feat(agentx): local models and the fallback in the app window`

### Task 15: Docs and the check on a real machine

**Files:**
- Modify: `docs/guides/cli.md`, `docs/whats-new.md` (a 3.10 section on top), `docs/guides/upgrading.md`, `docs/cookbook/patterns.md` (recipes: agentx on Ollama; LM Studio; the fallback; a bigger window with a Modelfile), `docs/cookbook/faq.md`, `docs/cookbook/troubleshooting.md`, `docs/concepts/models.md` (replace the hand-written `Ollama(BaseChatModel)` at 10 and 278-298 with `Local`), `docs/getting-started.md:62`, `docs/guides/yaml-config.md:72,159`, `README.md`, `CHANGELOG.md` (`## [Unreleased]`), `host/data.js` (regenerate with `py -3.12 host/build_data.py`)
- Test: `tests/test_docs*.py` if present; `node --check host/data.js`

- [ ] **Step 1: Write the docs.** Every new setting, command (`/fallback`), message and mark from Tasks 9-14, with the library API (`Local`, `fallback_model=`, `context_window=`, outcome `no_answer`). The version is not bumped (release is a separate, approved step).
- [ ] **Step 2: Run** `py -3.12 host/build_data.py && node --check host/data.js && pytest -q` — Expected: build writes data.js; full suite PASS.
- [ ] **Step 3: Check by hand on the requester's laptop** (Ollama running, `qwen3-agent` present): `agentx --model qwen3-agent` makes a small edit in a scratch folder; with `"fallbackModel": "qwen3-agent"` and an invalid `ANTHROPIC_API_KEY`, a `/team` job finishes on `qwen3-agent` with the marks. Record the outcome in the final report; do not commit anything from this step.
- [ ] **Step 4: Commit** `docs: local models and the fallback`
