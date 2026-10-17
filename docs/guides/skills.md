# Skills

A skill is a short guide for a kind of work: "how to explore a project", "how to change files safely". An agent
sees one line per skill in its prompt (the name and what it is for) and reads the full text only when it needs it,
by calling the `use_skill` tool. That keeps the prompt small and, because the list is built the same way every
time, identical from one request to the next, which is what lets the provider's prompt cache (Anthropic's
explicit cache, OpenAI's automatic prefix cache) reuse it.

## In the terminal app

`agentx` turns skills on by itself. The first time it runs it creates its built-in skills in
`~/.agentx/skills/`. Type `/skills` to see them.

| Command | What it does |
|---|---|
| `/skills` | List the skills found, where they came from (`bundled`, `own`, `installed`, `project`) and the folder they live in. |
| `/skills install <link>` | Install skills from `https://github.com/<owner>/<repo>` (optionally `/tree/<branch>/<folder>`) or from a folder on your computer. You are shown what was found and asked before anything is saved. |
| `/skills remove <name>` | Delete a skill from `~/.agentx/skills/` after a yes. |

A new conversation (`/clear`) picks up added or removed skills.

Only you can install a skill: the agent has no tool for it, and it cannot write to `~/.agentx` or to a project's
`.agentx` (those are always refused). A skill is instructions the agent will follow, so install only skills you
trust. Installing downloads the exact commit the link points to and records it; nothing updates by itself.
To update a skill, remove it and install it again.

## In your own code

```python
from agentx_dev import AgentRunner, AgentType, Permissions
from agentx_dev.Skills import Skills

runner = AgentRunner(
    model=llm, agent=AgentType.ReAct,
    permissions=Permissions.full_access(["./project"]),
    skills=True,                       # ~/.agentx/skills, built-in skills created there on first use
)

# or choose the folder:
runner = AgentRunner(model=llm, agent=AgentType.ReAct, tools=my_tools,
                     skills=Skills("./my-skills"))
```

Keep the skills folder **outside** the folders your agent's write tools can reach. A skill is instructions the agent
will follow; if the agent could write `~/.agentx/skills/x/SKILL.md` itself, it could plant one that every later run
trusts. The terminal app enforces this (its file tools can never write to `.agentx` or `~/.agentx`); with
`Permissions.full_access(...)` in your own code, it is up to you not to include the skills folder.

The first runner that uses a folder creates the built-in skills in it (and a small `.state.json` that remembers
which were created, so one you delete is not recreated). To use a folder as it is, pass `Skills(folder, seed=False)`.
`Skills(folder, project_dirs=[...])` adds read-only folders that are treated as untrusted, the way the terminal
app treats a project's `.agentx/skills`.

### Installing from your own code

The terminal app's `/skills install` is built from four functions you can call yourself. Nothing is saved until you
call `install`, so you decide what to show and what to ask:

```python
from agentx_dev.Skills import open_source, discover, preview_text, install, remove_skill

with open_source("https://github.com/obra/superpowers") as fetched:     # or the path of a folder
    found, problems = discover(fetched.root, fetched.subpath)
    print(preview_text(fetched, found, problems))
    if input("Install these? [y/N] ").strip().lower() == "y":
        names = install(found, "./my-skills", fetched)

remove_skill("brainstorming", "./my-skills")       # later, if you change your mind
```

`open_source` looks up the exact commit the link points to and downloads that commit (the files are removed when
the `with` block ends). `install` refuses a name that is already taken or is the name of a built-in skill, and in
that case installs nothing. Failures raise `SkillInstallError` with a sentence you can show. Only
`github.com` links and local folders are accepted.

`skills=None` (the default) changes nothing. The list of skills and the tool list are fixed when the runner is
built; to pick up a skill added later, build a new runner.

## The built-in skills

| Skill | About |
|---|---|
| `exploring-a-project` | `list_directory`, `find_files`, `grep`, `read_path` |
| `changing-files` | `write_file`, `edit_file`, `delete_path`, `move_file` |
| `running-code` | `run_python`, `run_shell` |
| `web-research` | `web_search`, `web_fetch` |
| `delegating-work` | `start_team_job`, `delegate` |

A skill that names tools is listed only when the agent has at least one of them. Tools that no skill names (your
own) are described together in one generated skill, `custom-tools`; it is built in memory and never written to disk.

You can edit a built-in skill. agentx will not overwrite your edit, and will not recreate a built-in skill you
delete. A copy you did not touch is updated when a newer agentx ships a newer version of it.

## Writing a skill

A folder with a `SKILL.md`:

```markdown
---
name: release-notes
description: How we write release notes - use when asked to summarise a release.
---
# Release notes

1. Group changes under Added / Changed / Fixed.
2. ...
```

The `name` is lower-case letters, digits and hyphens (at most 64). Keep the `description` to one sentence that says
when to use the skill: it is the only part of the skill that is always in the prompt. Other files in the folder
can be read by the agent with `use_skill(name, file="notes.md")`; they are never run for it. Skills written for
Claude work here as they are.

A project can keep its own in `<project>/.agentx/skills/`. Those are shown to the agent in a separate block marked
as coming from the project folder (which may be a cloned repository), cannot replace a skill of yours with the
same name, and cannot override the approval prompts.

## Limits

Skill text is cut at 20,000 characters, descriptions at 300 (200 in the prompt), and the list in the prompt at
6,000 characters in all (the last line says how many were left out). A download may be at most 20 MB, 3,000 files
and 50 skills.
