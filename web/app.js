// Journly front end. No framework, no build step.
(() => {
  "use strict";

  const sidebarEl = document.getElementById("sidebar");
  const entryListEl = document.getElementById("entry-list");
  const searchEl = document.getElementById("search");
  const editorEl = document.getElementById("editor");
  const entryDateEl = document.getElementById("entry-date");
  const wordCountEl = document.getElementById("word-count");
  const saveStateEl = document.getElementById("save-state");
  const themeToggleEl = document.getElementById("theme-toggle");
  const appEl = document.querySelector(".app");

  const AUTOSAVE_DELAY = 800;
  const THEME_KEY = "journly-theme";

  let today = new Date().toISOString().slice(0, 10);
  let currentDate = today;
  let entries = []; // full unfiltered list, newest-first
  let searchQuery = "";
  let saveTimer = null;
  let writingTimer = null;
  let loadToken = 0; // guards against out-of-order responses when switching fast

  // -------------------- utilities --------------------

  function formatLabel(dateStr) {
    // dateStr is YYYY-MM-DD; parse as local date, not UTC, to avoid off-by-one.
    const [y, m, d] = dateStr.split("-").map(Number);
    const dt = new Date(y, m - 1, d);
    return dt.toLocaleDateString(undefined, {
      weekday: "short",
      month: "short",
      day: "numeric",
      year: dt.getFullYear() !== new Date().getFullYear() ? "numeric" : undefined,
    });
  }

  function wordCount(text) {
    const trimmed = text.trim();
    if (!trimmed) return 0;
    return trimmed.split(/\s+/).length;
  }

  function setSaveState(state) {
    saveStateEl.classList.remove("visible", "error");
    if (state === "saving") {
      saveStateEl.textContent = "Saving\u2026";
      saveStateEl.classList.add("visible");
    } else if (state === "saved") {
      saveStateEl.textContent = "Saved";
      saveStateEl.classList.add("visible");
      window.clearTimeout(setSaveState._fade);
      setSaveState._fade = window.setTimeout(() => {
        saveStateEl.classList.remove("visible");
      }, 1500);
    } else if (state === "error") {
      saveStateEl.textContent = "Could not save";
      saveStateEl.classList.add("visible", "error");
    } else {
      saveStateEl.textContent = "";
    }
  }

  // -------------------- theme --------------------

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    themeToggleEl.textContent = theme === "light" ? "\u263C" : "\u263D"; // sun / moon
  }

  function initTheme() {
    const saved = localStorage.getItem(THEME_KEY);
    applyTheme(saved === "light" ? "light" : "dark");
  }

  function toggleTheme() {
    const next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
    localStorage.setItem(THEME_KEY, next);
    applyTheme(next);
  }

  // -------------------- data --------------------

  async function fetchEntries() {
    const res = await fetch("/api/entries");
    const data = await res.json();
    entries = data.entries || [];
    today = data.today || today;
    renderList();
  }

  async function loadEntry(dateStr) {
    const token = ++loadToken;
    currentDate = dateStr;
    entryDateEl.textContent = formatLabel(dateStr) + (dateStr === today ? "  \u2014  Today" : "");

    const res = await fetch(`/api/entries/${dateStr}`);
    const data = await res.json();
    if (token !== loadToken) return; // a newer load started while this was in flight

    editorEl.value = data.content || "";
    wordCountEl.textContent = `${wordCount(editorEl.value)} word${wordCount(editorEl.value) === 1 ? "" : "s"}`;
    setSaveState(null);
    renderList();
    editorEl.focus();
  }

  function flushSave(useBeacon) {
    window.clearTimeout(saveTimer);
    saveTimer = null;
    const content = editorEl.value;
    const dateStr = currentDate;

    if (useBeacon) {
      const blob = new Blob(
        [JSON.stringify({ date: dateStr, content })],
        { type: "application/json" }
      );
      navigator.sendBeacon("/api/save-beacon", blob);
      return;
    }

    setSaveState("saving");
    fetch(`/api/entries/${dateStr}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    })
      .then((res) => {
        if (!res.ok) throw new Error("save failed");
        return res.json();
      })
      .then((data) => {
        setSaveState("saved");
        updateLocalSummary(dateStr, data.preview, data.words, data.exists);
      })
      .catch(() => setSaveState("error"));
  }

  function updateLocalSummary(dateStr, preview, words, exists) {
    const idx = entries.findIndex((e) => e.date === dateStr);
    if (!exists) {
      if (idx !== -1) entries.splice(idx, 1);
    } else if (idx !== -1) {
      entries[idx] = { ...entries[idx], preview, words, updated: Date.now() / 1000 };
    } else {
      entries.unshift({ date: dateStr, preview, words, updated: Date.now() / 1000 });
      entries.sort((a, b) => (a.date < b.date ? 1 : -1));
    }
    renderList();
  }

  function scheduleSave() {
    window.clearTimeout(saveTimer);
    saveTimer = window.setTimeout(() => flushSave(false), AUTOSAVE_DELAY);
  }

  // -------------------- sidebar rendering --------------------

  function visibleEntries() {
    if (!searchQuery) {
      // Ensure today appears even if it has no file yet.
      const hasToday = entries.some((e) => e.date === today);
      const list = hasToday ? entries.slice() : [{ date: today, preview: "", words: 0, updated: null }, ...entries];
      return list;
    }
    return entries.filter((e) => e.date === currentDate || matchesSearch(e));
  }

  function matchesSearch(entry) {
    return searchResultsSet.has(entry.date);
  }

  let searchResultsSet = new Set();
  let searchPreviewOverride = new Map();

  async function runSearch(query) {
    searchQuery = query.trim();
    if (!searchQuery) {
      searchResultsSet = new Set();
      searchPreviewOverride = new Map();
      renderList();
      return;
    }
    const res = await fetch(`/api/search?q=${encodeURIComponent(searchQuery)}`);
    const data = await res.json();
    searchResultsSet = new Set((data.entries || []).map((e) => e.date));
    searchPreviewOverride = new Map((data.entries || []).map((e) => [e.date, e.preview]));
    renderList();
  }

  function renderList() {
    entryListEl.innerHTML = "";
    const list = visibleEntries();

    if (searchQuery && list.filter((e) => e.date !== currentDate || searchResultsSet.has(e.date)).length === 0) {
      const empty = document.createElement("div");
      empty.className = "empty-note";
      empty.textContent = "No matches";
      entryListEl.appendChild(empty);
      return;
    }

    for (const entry of list) {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "entry-item" + (entry.date === currentDate ? " active" : "");

      const dateLine = document.createElement("span");
      dateLine.className = "entry-item-date";
      dateLine.textContent = formatLabel(entry.date);
      if (entry.date === today) {
        const badge = document.createElement("span");
        badge.className = "entry-item-today";
        badge.textContent = "Today";
        dateLine.appendChild(badge);
      }

      const previewLine = document.createElement("span");
      previewLine.className = "entry-item-preview";
      const preview = searchPreviewOverride.get(entry.date) ?? entry.preview;
      previewLine.textContent = preview || "No entry yet";

      item.appendChild(dateLine);
      item.appendChild(previewLine);
      item.addEventListener("click", () => {
        if (entry.date === currentDate) return;
        flushSave(false);
        loadEntry(entry.date);
      });

      entryListEl.appendChild(item);
    }
  }

  // -------------------- keyboard shortcuts --------------------

  function isModKey(e) {
    return e.metaKey || e.ctrlKey;
  }

  function handleShortcut(e) {
    if (isModKey(e) && e.key.toLowerCase() === "n") {
      e.preventDefault();
      if (currentDate !== today) {
        flushSave(false);
        loadEntry(today);
      }
      return;
    }
    if (isModKey(e) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      flushSave(false);
      return;
    }
    if (isModKey(e) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      searchEl.focus();
      searchEl.select();
      return;
    }
    if (isModKey(e) && e.shiftKey && e.key.toLowerCase() === "l") {
      e.preventDefault();
      toggleTheme();
      return;
    }
    if (e.key === "Escape") {
      if (document.activeElement === searchEl && searchEl.value) {
        searchEl.value = "";
        runSearch("");
      }
      editorEl.focus();
    }
  }

  // -------------------- wire up --------------------

  editorEl.addEventListener("input", () => {
    const count = wordCount(editorEl.value);
    wordCountEl.textContent = `${count} word${count === 1 ? "" : "s"}`;
    scheduleSave();

    window.clearTimeout(writingTimer);
    appEl.classList.add("writing");
    writingTimer = window.setTimeout(() => appEl.classList.remove("writing"), 2500);
  });

  editorEl.addEventListener("blur", () => flushSave(false));

  window.addEventListener("beforeunload", () => {
    if (saveTimer) flushSave(true);
  });

  searchEl.addEventListener("input", (e) => runSearch(e.target.value));

  themeToggleEl.addEventListener("click", toggleTheme);

  document.addEventListener("keydown", handleShortcut);

  // -------------------- boot --------------------

  initTheme();
  fetchEntries().then(() => loadEntry(today));
})();
