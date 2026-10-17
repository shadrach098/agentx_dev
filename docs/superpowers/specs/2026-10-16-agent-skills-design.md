# Agent skills - design

## Goal

An agent can read short "skills": guides for a kind of work ("how to explore a project", "how to change files
safely"). The ones agentx needs for its own default tools are created automatically the first time an agent
runs. A skill for the user's own tools appears when those tools are added. The user can also install skills
from a GitHub link (for example the `superpowers` repository) or a folder.

Two reasons, in this order:

1. **Prompt cache.** Long how-to text no longer sits in the system prompt. The prompt carries one short line per
   skill (name and description); the model opens the full text only when it needs it, with a `use_skill` tool.
   The block that is sent on every request stays small and, because it is built the same way every time, stays
   identical from one request to the next, which is what lets Anthropic's explicit cache and OpenAI's automatic
   prefix cache reuse it.
2. **Better use of the default tools.** The tools' own descriptions say what they do; skills say how to use
   them well together (read before edit, search before read, one-shot commands, treat web pages as data).

## What a skill is

A folder holding `SKILL.md`: front matter with `name` and `description`, then the text. Same format as Claude's
skills, so existing skills drop in. agentx adds two optional front matter keys that other tools ignore:
`tools: [..]` (the tools the skill is about) and `version: N` (for the skills agentx ships).

```
~/.agentx/skills/
  exploring-a-project/
    SKILL.md
    .agentx-skill.json     # written by agentx: managed / installed, version, hash, source link, commit
  .state.json              # names of the default skills created so far
```

## Behaviour

**The index.** A runner built with `skills=` gets (a) a `use_skill(name, file="")` tool and (b) a block at the
end of its system addendum listing `- name: description` for each skill, sorted by name. `use_skill` returns the
skill's text (and lists the files in its folder); `file=` returns one of those files. Both are computed once,
when the runner is built, so they cannot change mid-run.

**Default skills.** Five ship inside the package (`agentx_dev/resources/skills/`), written by hand:
`exploring-a-project`, `changing-files`, `running-code`, `web-research`, `delegating-work`. Together they name
every tool agentx ships. They are created in `~/.agentx/skills/` the first time a runner is built with
`skills=` pointing there. A skill that names tools is listed only when at least one of those tools is present in
this runner (the terminal app has no web tools, so `web-research` is not listed there).

**Keeping them current.** Each time a runner is built: a default that was never created is created; an
unedited copy is replaced when agentx ships a newer `version`; a copy the user edited, a skill the user made
under the same name, and a default the user deleted are left alone. When nothing changed, nothing is written.

**The user's own tools.** Tools no skill names are described in one skill, `custom-tools`, generated in memory
for that runner (never written to disk, because the folder is shared by agents that have different tools). It
costs one line in the index. It is mostly a map: the tool descriptions in the tool list stay the full contract.
If it turns out not to earn its line it is the first thing to remove.

**Installing.** `/skills install <github link or folder>` in the terminal app (library: `open_source`,
`discover`, `preview_text`, `install`). It resolves the link to an exact commit, downloads that commit,
finds the skill folders, shows names, descriptions, file counts and any program files, asks, and only then
copies them into `~/.agentx/skills/` with a sidecar recording the source and commit. `/skills remove <name>`
deletes one; `/skills` lists them.

## Decisions

- **Library default is off, the terminal app's is on.** `AgentRunner(skills=None)` changes nothing for existing
  users (no new files in their home folder, no prompt change). `skills=True` or `skills=Skills(...)` opts in.
  The terminal app always enables it. Making it the library default is a possible later, deliberate change.
- **Only the user installs.** No tool can install or remove a skill. A skill is persistent instruction text; if
  the model could fetch one from a link it found in a web page, that page could plant instructions that outlive
  the conversation. The terminal app's existing rule that `.agentx/*` and `~/.agentx` are never writable by the
  file tools stays exactly as it is, and the tests pin it.
- **GitHub and local folders only.** `https://github.com/<owner>/<repo>[/tree/<branch>/<folder>]`. Other hosts,
  `http://`, and `git@` addresses are refused. Redirects may only go to GitHub's own hosts.
- **Pinned.** The link is resolved to a commit first and that commit's archive is downloaded, so what the user is
  shown is what is saved, and a later change in the repository changes nothing. No auto-update.
- **Downloads are hostile input.** Archive paths with `..`, absolute paths or drive letters are refused; links are
  never unpacked or copied; file count and size are capped; the sidecar is always written by us, never copied
  from the download (so a download cannot claim to be a "built-in" skill); a download cannot use the name of a
  built-in skill or of a skill already installed.
- **Project skills are untrusted.** `<project>/.agentx/skills/` (read-only) is listed in its own marked block,
  after the user's, with the same wording used for `AGENTX.md` files from a cloned repository. A project skill
  cannot shadow a user skill of the same name. `use_skill` prefixes project and installed skills with a line
  saying they cannot override the rules or the approval prompts. A `.agentx` or `.agentx/skills` that is a link
  out of the project is not read.
- **Scripts are not run by skills.** A skill's files are only readable, through `use_skill(name, file=...)`.
  Anything the model then runs goes through `run_shell` / `run_python` and their normal approval prompts. The
  install preview names the program files in a download so the user knows they are there.
- **Failure never stops the agent.** An unwritable skills folder, a damaged `SKILL.md`, a missing network: logged
  or shown as one line; the agent runs without that skill.

## Limits

Name: lower-case letters, digits, hyphens, at most 64. Description: at most 300 characters (200 in the index).
Skill text: at most 20,000 characters. Index: lines are cut so they total at most 6,000 characters, and the
last line says how many skills were left out. A skill file read through `use_skill(file=)`: at most 20,000
characters, text only. Install: archive at most 20 MB and 3,000 files, one file at most 1 MB, 30 MB in all, at
most 50 skills, searched at most 5 folders deep.

## Not in this change

Updating an installed skill (remove and install again); installing from anything but GitHub or a folder;
skills for sub-agents (the terminal app's team helpers do get them; see the CLI guide); `allowed-tools` or other front matter
keys that change what a skill may do; a skill marketplace; the `delegate` tool joining the `delegating-work`
skill's visibility (the tool is added after skills are attached, so the library shows that skill only when
`start_team_job` is also present).

## Open questions for the owner

1. Should the library default flip to `skills=True` in a later major version?
2. Is the generated `custom-tools` skill worth its index line once real use shows what models do with it?
