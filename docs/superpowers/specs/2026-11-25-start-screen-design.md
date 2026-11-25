# agentx: a start screen when the window opens outside a project

Date: 2026-11-25. Status: approved by the user ("yes build the start screen") as proposed in conversation.

## Intent

`agentx --ui` starts in the folder it was launched from and goes straight to a new conversation. That is right in a
project. When the folder is not a project (the user's home folder, a drive root such as `C:\`, or a shortcut that
starts the window without a folder), a conversation there is rarely what the user wants: they want to pick a folder
to work in. agentx already knows such a folder is not a project (`is_project_folder` in
`agentx_dev/cli/settings.py`: no work log, no folder settings) but does not act on it at start.

## What the user sees

In the app window, when agentx starts in a folder that is not a project and has not resumed a conversation
(`--continue` / `--resume` / a lane or device conversation), the window shows a start screen instead of the welcome:

```
                 Choose a folder to work in
   C:\Users\bruce is your home folder, not a project.

   ┌───────────────────────────────────────────────┐
   │ AgentX            C:\Users\bruce\Desktop\...   2 hours ago │
   │ legal_mate_2      C:\Users\bruce\Desktop\...   3 days ago  │
   │ ...  (up to 8 recent folders)                              │
   └───────────────────────────────────────────────┘
      [ Open a folder… ]          Chat here anyway
```

- **Recent folders**: the folders the user trusted before (`trusted_folders(home)` in `agentx_dev/cli/trust.py`),
  resolved to their real spelling (on Windows `Path.resolve()` restores the case the trust file lowercases), kept
  only when they still exist and are projects, sorted by when a conversation there was last saved (the mtime of
  `sessions_dir(home, folder)`; folders with no saved conversation last), at most 8. Each row: the folder name, its
  path (shortened in the middle when long), and how long ago it was used.
- **Picking a folder** (a row, or **Open a folder…** which asks for a path as the existing "Open another folder"
  does) starts agentx there in a window of its own, exactly as the folder menu's "Open another folder…" does
  (`WebFront.open_folder`), and then this agentx ends, the same way closing its window ends it, a few seconds later.
  Its window shows "agentx opened <folder> in a new window. You can close this one." The new agentx asks its own trust
  question if the folder is not trusted.
- **Chat here anyway** hides the start screen and shows the usual welcome; the conversation starts there as today.
- In a project folder, nothing changes.

## Goals and non-goals

- Goal: no accidental conversations in the home folder or a drive root from the app window.
- Non-goal: the terminal app (`agentx` without `--ui`) keeps today's behaviour. *(builder's call; the user asked about
  the window.)*
- Non-goal: a native folder picker dialog; a typed path, as elsewhere in the window, is enough.
- Anti-goal: never start agentx in a folder the user did not choose; never read anything from a listed folder beyond
  checking it exists (no settings, no notes, no files).

## Approaches considered

- *Switch this agentx's folder in place* (re-root the running app). Rejected: everything (gate, sandbox, settings,
  trust, MCP servers, skills) is built for one folder at start; re-rooting risks keeping state from the wrong folder.
- *Start a new agentx in the chosen folder, then end this one* (chosen). Reuses `open_folder`, which already starts
  agentx safely in another folder (nothing imported from it), and the existing "window closed → agentx ends" path.

## Left to the builder

Endpoint names and shapes; where the start-screen decision lives (server side, so a reload shows it again until a
choice is made); the "ago" wording; markup and styles in the existing visual language (light and dark, keyboard
reachable rows, visible focus, narrow widths); tests (server: the list, the decision, the open-then-end path with the
process start stubbed; window: a Playwright test like the other `tests/*_browser.py`); docs (`docs/guides/cli.md` app
window section) and a CHANGELOG `[Unreleased]` entry, `host/data.js` rebuilt.
