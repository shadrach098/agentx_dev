---
name: exploring-a-project
description: How to find your way around a project before changing it - list, find, grep, read.
tools: [list_directory, find_files, grep, read_path, recall]
version: 3
---
# Exploring a project

Look before you change anything. Work from wide to narrow:

1. `list_directory` on the project root to see the layout. Note a README, a manifest (pyproject.toml,
   package.json) and a tests folder.
2. `find_files` when you know part of a file name ("where is config.py", `**/*.py`). It skips noise folders
   such as .git and node_modules and lists the newest files first.
3. `grep` when you know a word that is in the code (a function name, an error message). It matches the text
   literally; pass `regex=True` for a pattern. Narrow it with `path` and `glob` instead of searching everything.
4. `read_path` once you know the file. Read a small file whole. For a big one pass `offset` and `limit` to read
   a slice; a very long file comes back in parts with a note on where to continue.
5. `recall`, when you have it, searches notes of earlier sessions in this project (what was asked, decided,
   changed). Use it when the user refers to earlier work, before searching the code for it again.

Habits that save time:

- Search first, then read only the files the search points to. Do not read a whole folder to find one function.
- Read a file before you edit it: the edit tools refuse to change a file you have not read in this conversation.
- If a search finds nothing, try a shorter or different word before deciding something does not exist.
- Say what you found with file names and line numbers, so the user can check it.
- Older results are left out of later requests to save tokens. When you read something you will need later,
  write the facts and line numbers down in your message. To check one again, grep the file or read just those
  lines with `offset` and `limit`; do not read the whole file again.
