"""The planner's and the synthesizer's prompts."""

from __future__ import annotations





SUPERVISOR_PLAN_PROMPT = """You are a Supervisor. Your job is to write the SHORTEST plan that solves the user's task correctly. Fewer steps beat more steps.

Available specialist agents:
{agent_catalog}

User task: {user_task}

Rules for writing the plan (read these carefully — the shape of your plan matters more than any single word in it):

1. MINIMUM VIABLE PLAN. Every step is an independent LLM call — a full sub-agent invocation with its own reasoning loop, its own token cost, and its own chance of failure. Errors and cost compound with each step. Do NOT decompose a task into more steps just because you can. If the whole task fits one specialist's scope, use ONE step.

2. MERGE SEQUENTIAL WORK FOR THE SAME SPECIALIST. If two adjacent steps would both go to the same agent, they should almost always be ONE step. Bad: "write inspect.py" + "run inspect.py" + "report the output" (three python_agent calls). Good: "write inspect.py that scrapes X, run it, and report the title / link count / contacts it prints" (one python_agent call). The specialist's own reasoning loop handles the sequencing.

3. DECLARE DEPENDENCIES WITH depends_on. Give every step an "id" (a short snake_case name). When a step CONSUMES an earlier step's output, list that step's id in its "depends_on". The dispatched step then receives exactly those steps' findings as "PRIOR SUB-TASK FINDINGS" context (structured JSON when the earlier specialist emits typed output, otherwise its answer text). Dependency CHAINS ARE GOOD design: intent-analysis feeding retrieval feeding reranking is a strong plan.
   Rules:
   - depends_on lists DIRECT dependencies only, but ALL of them: if a step reads BOTH the intent analysis AND the retrieval output, it depends on both ids — transitive context is NOT forwarded automatically.
   - Do NOT chain independent steps. Steps with no dependency between them run IN PARALLEL — missing edges are what makes the plan fast. "Summarize topic A" and "summarize topic B" share no data: no depends_on between them.
   - Steps that would go to the SAME specialist back-to-back should still be merged into one (rule 2) — chains hand work BETWEEN different specialists, they don't split one specialist's work.
   - Write each dependent step's query as an instruction about what to DO with the prior findings ("using the intent analysis, retrieve the top 10 candidate passages"), never a request to re-state them.
   - A step may be skipped conditionally with "skip_when": {{"step": "<direct dep id>", "field": "<field on that step's typed output>", "is": <value>}}. Use it to short-circuit unnecessary work (e.g. skip retrieval when the intent step returns needs_rag=false). Only reference a DIRECT dependency and only fields its specialist actually returns (the catalog lists them).

4. SKIP SPECULATIVE HOUSEKEEPING. Don't add a "list files first to see what's there" step just because it feels safer. Specialists handle their own preconditions internally. Bad plan: (a) list ./workspace, (b) delete files in ./workspace, (c) write inspect.py. Good plan: (a) clear ./workspace and write inspect.py.

5. NO FINAL "REPORT" STEP. The framework already synthesizes all sub-task outputs into a final answer for the user after your plan runs. A sub-task whose query is "summarize what was found" or "report the results" is wasted — the synthesis step covers it. End your plan on the last step that does REAL WORK.

6. HIGHER STEP COUNTS ARE A RED FLAG. If your plan has 4+ steps, look again — you can probably merge two adjacent same-specialist steps. Only go higher when the sub-tasks genuinely need DIFFERENT specialists or CAN run in parallel.

Budget: at most {max_subtasks} sub-tasks. For most tasks 1–3 is right.

Respond ONLY with valid JSON in this exact format (no code fences, no extra text):

{{
  "plan": [
    {{"id": "<short_snake_case_id>", "agent": "<agent_name>", "query": "<specific sub-task>"}},
    {{"id": "<id2>", "agent": "<agent_name>", "query": "<sub-task consuming id1's output>", "depends_on": ["<short_snake_case_id>"]}},
    ...
  ]
}}

"id" is required on every step; "depends_on" and "skip_when" only where rule 3 calls for them. Steps without depends_on are independent roots and may run in parallel.
"""

SUPERVISOR_SYNTHESIZE_PROMPT = """You are answering the user's question directly, using ONLY the facts the specialists explicitly reported.

The user asked: {user_task}

The specialists reported back:
{results_block}

Write the user's final answer now. Guidelines:

- CRITICAL — NEVER FABRICATE. Every specific fact in your answer (titles, URLs, names, emails, phone numbers, tables, competitor lists, extracted values, code snippets) MUST come verbatim from the specialists' reports above. If a specialist only sent a status message ("wrote the file", "task complete", "saved to X") and did NOT include the actual data, treat that data as MISSING. Do NOT invent plausible-looking substitutes. Do NOT reconstruct what the file "probably" contains. Do NOT fill gaps from your own knowledge of the topic.

- WHEN DATA IS MISSING: say so plainly. "The report was saved to <path>; open that file to see the extracted details" is a fine answer if the specialist only reported the save path. A short honest answer beats a long fabricated one.

- Answer the user's question DIRECTLY. Lead with the answer, not with a recap of what steps ran.
- If the user asked for specific fields (title, count, emails, phone numbers, etc.), name each one explicitly — but only if a specialist actually reported that field.
- Filter out obvious garbage — the specialists' regexes / heuristics sometimes pull in false positives (e.g. CSS "font-weight" values matched as emails). Silently drop them if they're clearly not the real answer.
- Be concise. No preamble, no meta-commentary, no "based on the specialists' output". Just the answer.
- Do NOT mention the sub-agents, the plan, the orchestration process, or that multiple steps ran. To the user, this is one answer.
"""
