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

## Password protection

The first time you open Journly in a browser, you'll be asked to create a
password. After that, every visit shows a lock screen until you enter it.

- The password is never stored in plaintext — only a salted PBKDF2 hash
  lives in `.journly_auth.json` (gitignored, permissions locked to your
  user).
- Closing the tab, closing the browser, refreshing, or navigating away all
  end your session immediately — reopening Journly always asks for the
  password again. A 30-minute absolute limit is a fallback safety net for
  crashes or force-quits where that can't fire.
- Click the lock icon (🔒) in the sidebar to lock manually at any time
  without closing anything.
- To change your password, set a new one from your own terminal (not
  through the browser, so it's never typed anywhere but your own machine):

  ```sh
  python3 journly.py --set-password
  ```

  This prompts for the new password with hidden input and immediately
  signs out any existing browser sessions.

Note: this protects casual access to the app in a browser on your machine.
It's not encryption — the `.md` files in `entries/` are still plain text on
disk, and this is a local-only server, not something to expose to a
network.

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
