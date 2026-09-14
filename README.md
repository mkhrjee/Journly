# Journly

A lightweight, distraction-free journal. It's a text editor with a sidebar —
nothing more. One entry per day, one typeface, two themes, and your words
saved as plain Markdown files you own.

## Run it

Requires only Python 3 (standard library, no packages to install).

```sh
./journly
```

This starts a local server on `127.0.0.1` and opens your browser to it.
Nothing leaves your machine — the server only listens on localhost.

Options:

```sh
./journly --port 9000     # use a specific port (falls back to the next free one)
./journly --no-browser    # don't auto-open a browser tab
```

Or run it directly: `python3 journly.py`.

## Where your entries live

Every day gets one file: `entries/YYYY-MM-DD.md`, plain Markdown, readable
and greppable with any editor. This folder is gitignored — your journal is
never committed alongside the app code. Back it up, sync it, or `grep` it
however you like.

An entry is only written to disk once it has real content; clearing an
entry back to empty removes its file so the sidebar never fills with blank
days.

## Using it

- The sidebar lists entries newest-first, with today pinned at the top.
- Type in the editor — it autosaves as you go (about 800ms after you stop
  typing, and immediately when you switch entries, blur the editor, or
  close the tab).
- Search the sidebar box to filter entries by content.

### Keyboard shortcuts

| Shortcut | Action |
| --- | --- |
| `⌘N` / `Ctrl+N` | Jump to today's entry |
| `⌘S` / `Ctrl+S` | Save immediately |
| `⌘K` / `Ctrl+K` | Focus search |
| `⌘⇧L` / `Ctrl+Shift+L` | Toggle light/dark theme |
| `Esc` | Clear search and return to writing |

## What it isn't

No accounts, no sync, no markdown preview, no export, no tags, no plugins,
no font picker. If you want to change how your entries are stored, the
whole storage layer is under 100 lines in `journly.py` — it's meant to be
readable.
