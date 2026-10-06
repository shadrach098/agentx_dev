# Troubleshooting

Common errors and their fixes.


> **Both providers work.** Every `Claude()` in this page also works
> with `GPT()`. Same tools, same agent code, same runner APIs. Set
> whichever API key you have (`ANTHROPIC_API_KEY` for Claude,
> `OPENAI_API_KEY` for GPT) and swap the constructor. See
> [chat models](../concepts/models.md) for adding other providers.

## Import errors

**`ImportError: No module named 'anthropic'`**
Install the extra: `pip install agentx-dev[anthropic]`.

**`ImportError: No module named 'mcp'`**
Install the extra: `pip install agentx-dev[mcp]`.

**`ImportError: No module named 'agentx_dev.MCP'`**
Same fix. The framework imports MCP lazily; only fails on use.

## Model construction

**`OpenAI 400: Invalid value for 'parallel_tool_calls': 'parallel_tool_calls' is only allowed when 'tools' are specified.`**
Don't set `parallel_tool_calls=True` on the `GPT` model. It defaults on
for tool-using calls; setting at the model level leaks into tool-less
calls. Remove the arg.

**`AuthenticationError`**
Check your `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` env var.

**`ValueError: The 'model' object must inherit from BaseChatModel`**
You passed a raw provider client. Wrap it in a `BaseChatModel`
subclass.

## Model parameters *(3.4)*

**`400 Unsupported value: 'reasoning_effort' does not support 'none' with this model`**
Different model generations accept different `reasoning_effort` values.
Since 3.4 the model moves to the nearest supported value automatically
and logs a WARNING. On an older build, upgrade — or set a value from the
list in the error. If you passed `adapt_params=False`, the error is
deliberate: choose a listed value.

**`400 Unsupported parameter: 'max_tokens' ... Use 'max_completion_tokens' instead`**
Reasoning models renamed the parameter. 3.4 sends it under the new name
automatically (up front for `o1`/`o3`/`o4`/`gpt-5*`, and learned from the
error for anything newer).

**`400 temperature and top_p cannot both be specified for this model`**
Newer Claude models accept one or the other. 3.4 drops `top_p` and keeps
your `temperature`. Or set only one.

**`400 max_tokens: 64000 > 4096, which is the maximum allowed ...`**
The model has a lower output cap. 3.4 clamps `max_tokens` to it.

**`400 Function tools with reasoning_effort are not supported for <model> in /v1/chat/completions`**
The model can't take function tools together with reasoning on chat
completions, and often rejects the `'none'` the message suggests. Since
3.4.1, `GPT` switches that model's tool calls to the Responses API
automatically and logs a WARNING. On 3.4.0, upgrade. If you set
`use_responses_api=False`, the error is deliberate. Remove that, or pass
`use_responses_api=True`.

**`with_structured_output(...)` fails on a model that needs the Responses API**
Structured output forces a single tool call. Since 3.4.2, if the
Responses API rejects that forced choice, `GPT` retries with
`tool_choice="required"`, which picks the same tool when there's only
one, and remembers that for the model. Upgrade from 3.4.1.

**The log shows the old `Function tools with reasoning_effort ...` error but the call worked**
Before 3.4.2 the retry wrapper logged the original chat-completions
rejection at WARNING, even when the call then succeeded through the
Responses API. It's DEBUG now; the switch has its own WARNING, and a real
failure is still logged at ERROR.

**A WARNING says my setting was changed, but I need the exact value**
The model doesn't support it — no retry can make it. Choose a model that
does, or pass `adapt_params=False` to fail instead of adjusting.

**Still a `400` after upgrading**
Only parameter-compatibility errors are adjusted. Context-length
overflows, malformed messages, and content-policy refusals raise as
before. Read the provider's message — it's passed through unchanged.

## Media *(3.4)*

**`ValueError: Claude does not accept audio input`**
Claude has no audio input. Transcribe the audio first, or send it to an
audio-capable GPT model.

**`ValueError: GPT (chat completions) cannot fetch a document from a URL`**
Since 3.4.1 `GPT` sends document URLs through the Responses API, so you
only see this with `use_responses_api=False`, or when calling
`content_for_openai` yourself. Pass the file inline instead:
`Media.document("report.pdf")`.

**`ValueError: Media from raw bytes needs media_type=`**
Bytes carry no file type. Say what it is:
`Media.image(data, media_type="image/png")`. The exception is
`Media.document(data)`, which assumes `application/pdf`.

**`FileNotFoundError: Media file not found: bruce.jpeg`**
Media paths are relative to where your program runs, **not** the
agent's workspace. Use `./my_workspace/bruce.jpeg` or an absolute path.

**The model says it can't see the image**
The image was never attached. Naming a path in the prompt, or having the
agent call `read_path` (a text reader), doesn't show the model anything.
Pass the file with `media=`.

**`ValueError: sales.csv is 1,234,567 characters as text, over max_chars=100,000`**
Too big to send as text. Pass `Media.text(path, truncate=True)` to send
the first part, raise `max_chars=`, or — better for real datasets — let
an agent load the file with pandas through `run_python`.

**`ImportError: Reading spreadsheets needs pandas and openpyxl`**
pandas and openpyxl install with agentx-dev since 3.4.2. If they're
missing (an older install, or a different environment), run
`pip install -U agentx-dev pandas openpyxl`. `.xls` files also need
`xlrd`, and `.ods` files need `odfpy`.

**`ValueError: Word files aren't accepted by GPT or Claude`** (or PowerPoint, or archives)
Neither provider reads these. Save the file as PDF and send it with
`Media.document("file.pdf")`.

**A `.txt` file failed on Claude with a 400 about documents** *(3.4.0 / 3.4.1)*
Those versions sent text files as base64 documents, which Claude only
accepts for PDFs. 3.4.2 sends them as text. Upgrade; saved histories
with the old shape are converted automatically.

**A provider `400` about image size or format**
Providers cap image size (about 5 MB for Claude, 20 MB for GPT) and
accept a set of formats (PNG, JPEG, GIF, WebP). The framework doesn't
resize; shrink or convert the image first.

## Runner errors

**`TypeError: AgentRunner received both 'Agent' and 'agent'`**
Pick one — new code should use lowercase `agent=`.

**`ValueError: use_function_calling and bind_tools_natively are mutually exclusive`**
Pick one. Function calling routes through the parser; native binding
skips it.

**`ValueError: The 'Agent' object must be a template string containing '{tools}','{tool_names}',{user_input}`**
Your custom template is missing one of those placeholders. All three
are required.

**The agent returns "I'll do X. Just a second!" instead of doing X**
The model announced an action and ended its turn without calling a tool.
The framework re-prompts it once by default (`text_turn_nudges=1`) — if
you still see the preamble as the answer, the model ignored the nudge on
a hard task: bump `max_iterations`, or add a specialist `system_addendum`
spelling out which tool to call. Note the nudge only fires when the
runner has tools; a tool-less chat agent correctly returns prose.

**`ERROR: Invalid \escape: line 1 column …` from a tool call**
The model put a code snippet or a Windows path in a tool argument, which
isn't valid JSON (`\d`, `\U`, `C:\…`). The framework repairs the common
cases and otherwise feeds the error back for the model to resend — it no
longer crashes the run. If you still hit it, you're on a build older than
3.1.4; upgrade.

## Long runs and stuck agents *(3.5)*

**`result.outcome == "stuck"`**
The agent tried every recovery step and the same trouble came back.
`result.content` says what it was; `result.progress["failed"]` lists the
failed calls. Usually the task or a tool needs changing (a missing
permission, an unclear instruction). Raise `max_reflections` only if the
failed attempts show it was making different attempts each time.

**`result.outcome == "out_of_time"`**
`max_minutes` passed. Raise it, or split the task. The report shows how
far it got.

**`result.outcome == "out_of_budget"`**
The model's cost cap was reached. The cap is cumulative per model object
(everything that object has spent, not just this run), so a model reused
across runs may be nearly spent before the run starts. Use a fresh model
object, or raise `budget_usd`.

**A Supervisor step ends `iteration_limit` or `stuck`**
Since 3.5 a specialist that gave up is retried once with feedback (see
`max_subtask_retries`) and then flagged in `result.subtasks[i].error`
instead of being reported as a success. To make the Supervisor replan
around it instead, pass `persistence=Persistence(...)`.

**My tool ran once, but the agent called it twice**
That's persistent mode doing its job: the tool-result cache is off while
`persistence` is set, so every call executes.

**`spawn refused: no usable tools`**
A helper asked for tools and none are inside the ceiling. Add the preset
(for example `"files"` or `"code"`) to `SpawnConfig(capabilities=...)` or
put your tool in `SpawnConfig(tools=[...])`. The persistent default
allows only `web` and read-only files.

**`delegation refused: spawn limit reached; do this yourself`**
`max_spawns` counts planner-created helpers and `delegate` calls
together, per run. Raise it in `SpawnConfig`, or have the specialist do
the work itself.

**A helper I spawned isn't on the supervisor after the run**
Helpers last for one run (since 3.6). The record is in
`result.spawned`; to keep an agent, register it in `agents=`.

**A delegated helper keeps coming back `[delegate failed: ...]`**
It gave up or crashed; the message after the colon says how, and
`runner.spawned` / `result.spawned` hold its outcome. Give it a clearer
`task` (it sees nothing else) or different `instructions`.

## Tool errors

**Model calls the wrong tool**
- Improve descriptions.
- Say "Prefer over X when ..." and "Do NOT use for ..." explicitly.
- Reduce tool count; the more tools, the noisier the choice.

**`ToolError: refused: 'foo' has been called N times in a row`**
Dup-guard tripped. Fix the model's prompt so it uses prior results
instead of re-issuing. Change `max_iterations` if the model needs to
try more DIFFERENT calls.

**`TimeoutError: tool exceeded Ns timeout`**
Set a larger `tool.timeout_sec` on the specific tool, or make the tool
async and use `AsyncAgentRunner` so timeout cancels cleanly.

**`ToolError: circuit breaker open for 'X': tripped after N consecutive failures`**
The tool has been failing. Wait `recovery_timeout_sec` and it enters
half-open state (one probe call allowed). If the underlying issue is
fixed, call `runner.registry.get_breaker('X').reset()`.

## Permission / sandbox errors

**Tool "read_path" not available**
The `read_files` capability isn't granted in your `Permissions`. Set
it to `True`.

**`PermissionError: path outside sandbox`**
The path resolves outside `allowed_paths`. Either widen the sandbox or
use a path inside it.

**`PermissionError: path '/bruce.jpeg' (resolved to C:\bruce.jpeg) is outside the allowed sandbox`**
Before 3.4, a leading slash meant the root of the current drive, not the
workspace. Upgrade — with a workspace set, `/bruce.jpeg` now means the
workspace's `bruce.jpeg`. If no workspace is set (only `allowed_paths`),
set one, or drop the leading slash.

**`read_path` finds a file but `run_python` can't open it**
Before 3.4 `run_python` started in the directory your program was
launched from, not the workspace. Upgrade. Inside Python code, don't
start paths with `/` — to Python that's the filesystem root. Use
`open("bruce.jpeg")` or the workspace's absolute path.

**`ERROR: file is not utf-8 text` when reading an image**
`read_path` reads text. To have the model look at an image or PDF,
attach it with `media=` — see [Media](../media/overview.md).

**`.agentx/permissions.json` refuses to load**
Check file mode (should be 0o600, only readable by you). Fix:
`chmod 0600 .agentx/permissions.json`.

## Handoff errors

**`(handoff to unknown agent 'X' — no route)`**
Your agent tried to hand off to a name not in the coordinator's dict.
Add the missing agent or fix the handoff tool's `target=`.

**`(handoff loop exceeded max_hops=N)`**
Two agents ping-pong. Increase `max_hops`, or reshape the specialists
to break the cycle.

## Supervisor errors

**A sub-task fails and the supervisor gives up immediately**
By default the supervisor retries a *raised* sub-task once
(`max_subtask_retries=1`), feeding the error back so the specialist can
fix it. If a sub-task still fails after retries, its error shows in
`result.subtasks[i].error` and synthesis reports the data as missing.
Raise `max_subtask_retries` for flaky specialists; set it to `0` to
restore quit-on-first-failure. Note only sub-tasks that *raise* are
retried — one that returns thin/empty content is accepted as-is.

**OpenAI 400: "messages with role 'function' must have a 'name'"**
Fixed in 3.1's `_sanitize_history_for_next_agent`. If you see it, you
may be on an older build; upgrade.

**A step reports `skipped=True` and never ran *(3.3)***
Two causes, distinguished by `error`:

- `error` **set** — a dependency failed after `max_subtask_retries`, so
  this step was cascaded out. The message names the failed dependency.
  This is intentional: dispatching a step whose input is missing burns
  tokens to produce garbage. Independent branches keep running and
  synthesis reports over what survived.
- `error` **is None** — a `skip_when` condition matched. The reason is
  in `content`.

**A `skip_when` step ran when it should have been skipped *(3.3)***
`skip_when` evaluation is deliberately **fail-open**. A malformed
condition, a missing field, a dotted path that doesn't resolve, or a
dependency with no typed output all run the step rather than dropping
it. Check that the dependency actually declares an `output_schema` and
that `field` names a real field on it — conditions are evaluated in
Python against `SubtaskResult.output`, not against prose.

**My `depends_on` edges disappeared from the plan *(3.3)***
Plans are sanitized before execution and repairs never fail a run:
missing or duplicate step ids are auto-assigned, unknown dependencies
are dropped (a planner typo degrades to a root step rather than
deadlocking), self-dependencies are dropped, cycles are broken
deterministically at the back-edge in plan order, and spawn steps can't
be depended on. If anything was repaired the plan is re-requested once
(`max_plan_retries`, default `1`) with the warnings appended; if the
retry isn't better, the sanitized original is used. Run with
`verbose=True` to see the repair notes.

**Everything runs sequentially even though I'm on `AsyncSupervisor` *(3.3)***
Check three things: `sequential=True` is sugar for `max_parallel=1`;
`max_parallel=N` caps concurrency; and a plan where every step depends
on the previous one *is* a chain — the scheduler can only run what the
edges permit. Register specialists with no `depends_on` if they're
genuinely independent.

**My run hangs forever on a question in Jupyter *(3.6)***
A tool that asks through `CONIN$` (Windows) or `/dev/tty` (POSIX), like the
old `ask_human_tool` in `examples/mcp_github_triage_demo.py`, reads the
console of the kernel process, not the notebook. The prompt shows up in a
window nobody watches and the read blocks. Use `ask_user=True` on the
Supervisor, or `agentx_dev.ask_human_tool()` on a standalone runner: both
use the notebook's input box. The Interrupt button stops a run that is
waiting on it.

**`ask_user=True` on a server never asks *(3.6)***
Headless means no answer, by design: with no notebook, no terminal and no
controlling terminal there is nobody to ask, so each ask comes back
`no_channel` (see `result.asked`) and the agent proceeds on an assumption.
Pass a function instead (`ask_user=my_function`) that routes the question
through your own channel.

**An `async def` `ask_user` function gets reason `error` *(3.6)***
A sync specialist under `AsyncSupervisor` asks through the synchronous
path, which cannot await an async function, so that question is recorded
with reason `error` and the agent proceeds on an assumption. The planner
and spawned helpers on an `AsyncSupervisor` are async and can use it. Use
async specialists (`AsyncAgentRunner`), or pass a plain function.

**After a timeout, later asks get no answer *(3.6)***
When a built-in read in a terminal or on the controlling terminal times out
(`ask_timeout`, or the 300 s default on the controlling terminal), its
reader thread keeps waiting for input. Until that read returns, later timed
built-in asks in the same process are refused with reason `no_channel`
rather than started behind it. Answer the pending prompt, or raise
`ask_timeout` so it does not expire while someone is still there.

## Structured output

**`completion.output` is `None`**
The runner had no schema. Either pass `output_schema=` on the
constructor (applies to every call) or per-call on `invoke()`. A
per-call schema wins over the constructor one.

**`AgentRunner.__init__() got an unexpected keyword argument 'output_schema'`**
Constructor `output_schema` landed in 3.2.0. You're on an older build —
`pip install -U agentx-dev`. If you installed inside a notebook,
restart the kernel; the old module stays imported otherwise.

**`ValueError: output_schema=X: final answer is not JSON`**
Coercion has two strategies. Native function calling is preferred — the
schema goes to the model as a forced tool call, so the provider's
constrained decoding fills the fields and nothing is parsed out of
prose. Text JSON parsing is the fallback, used when the model has no
`call_with_tools` implementation or the forced call failed. This error
means you're on the fallback path and the model wrote prose. Fixes, in
order of preference: use a provider with native function calling, or
tell the agent in its prompt to end with JSON only.

**`ValueError: output_schema=X: parsed JSON did not match the schema`**
The model produced JSON with the wrong shape. The message includes the
parsed dict — compare it against your field names. The usual cause is a
`Field()` whose description was passed positionally: `Field("the user's
intent")` sets the *default*, not the description, so the model never
sees the hint. Use `Field(description="the user's intent")`.

**`SubtaskResult.output` is `None` inside a Supervisor**
The Supervisor reads `completion.output` off the specialist — it doesn't
coerce anything itself. The schema has to be declared on the *runner*
(`AgentRunner(..., output_schema=X)`), or on the registry entry via
`Specialist(output_schema=X)`, which is display-only for the planner
catalog and falls back to `runner.output_schema` when unset. If
coercion raises, the sub-task takes the error path and is retried per
`max_subtask_retries`.

## Cost / rate limit

**`CostBudgetExceeded: spent $X, limit $Y`**
Increase `budget_usd` or reduce the task. The agent halted the moment
cumulative spend crossed the cap.

**`RetryBudgetExceeded`**
The upstream keeps returning transient errors. Investigate rate
limits, quotas, or transient outages upstream. Reset with:

```python
llm._retries_used = 0
```

## Cache issues

**Same query returns stale results**
Cache TTL hasn't expired. Clear it: `runner.registry.cache.clear()` or
skip cache for this tool.

**Cache never hits**
Args differ across calls even for what looks like the "same" query.
The cache key is `(name, sorted-json-args)` — check for whitespace,
casing, ordering differences.

## Session issues

**`RuntimeError: Session has no runner attached`**
Call `session.attach(runner)` before any invoke.

**`ValueError: Session file X has schema version 2, but this build only knows up to 1`**
Upgrade `agentx-dev`, or downgrade the session file.

**`ValueError: Session file X is not valid JSON`**
File got corrupted (crashed write, wrong encoding). Check the `.tmp`
sibling — the framework writes atomically so a partial write should
leave `.tmp` behind, not corrupt the main file.

## Memory / RAG issues

**`ValueError: Embedding dim mismatch: store has X, new vectors have Y`**
You switched embedding models mid-collection. Reindex from scratch
with the new backend, or use a separate store.

**`SemanticMemory` returns nothing**
- Did you call `mem.set_query(...)` before `get_messages`?
- Is `min_score` too high for your embedding backend? Try `0.0` first
  to see what's actually being scored.
- Is the store empty? Check `len(mem._store)`.

**`vector_search` returns irrelevant results**
- Try `OpenAIEmbeddings` instead of `HashEmbeddings` — much better
  recall.
- Chunk size too large or too small. 500-1500 char chunks with 100-300
  char overlap is a reasonable starting point.
- Add source-file metadata so the LLM can weigh authority.

## Streaming issues

**Nothing prints**
`print(..., flush=True)` or the console buffers.

**Streaming stops mid-response**
Provider timeout or connection drop. Retry or lower the effective
response length.

**`text_delta` events never fire**
You're in `use_function_calling=True` mode. Text-delta events only
fire in text mode.

**Supervisor / Handoff `stream()` events don't arrive to my UI *(3.1)***
The `completion` (Supervisor) and `result` (Handoff) events carry
non-JSON-serializable dataclasses (`SupervisorResult`,
`HandoffResult`). Serialize their `.completion.content` and
`.hops` fields explicitly before yielding to SSE / websockets.
See patterns §21.

## Prompt caching *(3.1)*

**`cache_hit_ratio` stays at 0.0**
Prompt caching is Anthropic-only. Set `Claude(enable_prompt_cache=True)`.
If it's on and still 0.0: (a) first call always misses (creates the
cache); (b) 5-minute TTL expired between calls; (c) system prompt or
tool list changed between calls — any drift invalidates the cache.

**Anthropic 400 on `cache_control`**
Your anthropic SDK is too old. Bump to `>=0.36`:
`pip install -U 'anthropic>=0.36'`.

## Batch API *(3.1)*

**`RuntimeError: This anthropic SDK version does not expose the Batch API`**
Same as above — upgrade `anthropic>=0.36`.

**`TimeoutError: batch did not finish within Ns`**
Bump `max_wait_sec` (Anthropic's TTL is 24h, so up to 86400). For
huge batches, poll less often (`poll_interval_sec=60`) to reduce
request overhead.

**Some results come back as `{"error": ..., "type": "errored"}`**
Per-request failure — the batch itself succeeded. Loop over the
results and dispatch dicts vs. strings:

```python
for i, r in enumerate(results):
    if isinstance(r, dict):
        print(f"[{i}] {r['type']}: {r['error']}")
    else:
        process(r)
```

## Compiled optimizer *(3.1)*

**`Compiled.compile()` never improves over baseline**
- Trainset too small (< 5 cases) — teacher can't see enough failure
  signal. Add more cases.
- Assertions too permissive — everything passes baseline. Tighten
  assertions so the baseline actually fails some cases.
- Teacher model is too weak — pass a stronger `teacher_model=` (Opus
  or Sonnet, not Haiku) via the constructor.

**Best addendum overfits — helps train, hurts prod**
Split your suite into `trainset` (fed to `Compiled`) and `holdset`
(evaluated with `EvalRunner` after compile). Only ship the tuned
addendum if `holdset` score improves too.

## Vector store adapter errors *(3.1)*

**`ImportError: ChromaVectorStore requires the chromadb package`**
Install the extra: `pip install agentx-dev[chroma]` (or `[qdrant]` /
`[pgvector]`).

**`ValueError: Embedding dim mismatch` after switching adapters**
The adapter shape is identical but the *backend* stores vectors on
disk / server. Reindex from scratch when moving between adapters or
between embedding models.

**Qdrant: `UnicodeError` / IDNA failure when pointing at a local folder**
You passed a filesystem path to `location=`, which qdrant-client parses
as a hostname. Use `path="./.qdrant"` for file-backed local persistence
(3.1.6). Passing both `path=` and `url=`/`location=` raises a
`ValueError` — they're mutually exclusive.

**`Exception ignored in: <function QdrantClient.__del__>` at exit**
Harmless interpreter-shutdown noise from qdrant-client, not an AgentX
error and not data loss — the local client's destructor runs after
modules it depends on have already been torn down. The store owns its
client privately and exposes no `close()`. If you need lifecycle
control, build the client yourself and hand it in:

```python
from qdrant_client import QdrantClient
client = QdrantClient(path="./.qdrant")
store = QdrantVectorStore(embeddings=embeddings, client=client)
...
client.close()          # now the shutdown is yours to order
```

**Qdrant: "collection X vector dim Y != expected Z"**
The collection was created with a different embedding dim. Delete
the collection first (`client.delete_collection(name)`) and let the
adapter recreate it.

**Postgres: `CREATE EXTENSION vector` fails**
The `pgvector` extension isn't installed on the DB. On managed
Postgres (RDS / Cloud SQL) you must enable it via console first;
on self-hosted, `apt install postgresql-16-pgvector` (or equivalent).

## Debug tooling

**Enable verbose logging:**

```python
import logging
logging.getLogger("agentx_dev").setLevel(logging.DEBUG)
```

**Enable observability:**

```python
from agentx_dev import config, observability, ConsoleHook
config.observability_enabled = True
observability.add_hook(ConsoleHook(verbose=True))
```

**Inspect the working history:**

```python
result = runner.invoke("...")
for m in result.history:
    print(f"[{m['role']}] {str(m.get('content'))[:120]}")
```

**Inspect every tool call:**

```python
for tc in result.tool_calls:
    print(f"{tc.name}({tc.args})")
    print(f"  -> {str(tc.result)[:120]}")
```

**Trace the plan (Supervisor):**

```python
supervisor = Supervisor(model=llm, agents=..., verbose=True)
```

**Trace hops (Handoffs):**

```python
result = coord.run("...")
print(result.hops)
```
