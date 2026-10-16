---
name: changing-files
description: How to change files safely - small edits, new files, when to rewrite, and what asks for approval.
tools: [write_file, edit_file, delete_path, move_file]
version: 1
---
# Changing files

Make the smallest change that does what was asked, and keep the file's existing style.

- `edit_file` replaces exactly one occurrence of some text. Read the file first. Copy the old text exactly, with
  enough surrounding lines to be unique. If it says the text is missing or appears more than once, read the file
  again and adjust; do not guess.
- For a bigger change (rewriting a function, several separate edits) use `write_file` with the full new contents
  instead of chaining many edits.
- `write_file` on a path that already exists refuses unless you read the file first. Use `if_exists="rename"` for
  a deliverable that must not replace an existing file, `"overwrite"` only when the user asked to replace it, and
  `"append"` to add to the end.
- `delete_path` and `move_file` are the risky ones. Say what you are about to remove or move and why, and never
  use them to "tidy up" beyond what was asked.
- Changes are shown to the user for approval. If one is declined, do not retry the same thing: adjust, or ask
  what they want.
- After a change, check it: read the changed part again or run the project's tests.
