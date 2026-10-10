const STATE_KEY = "jazzArchive.state.v1";
const RESULT_LIMIT = 200;
const STATUS_LABELS = { "to-listen": "To listen", listened: "Listened" };

const elements = {
  search: document.querySelector("#search"),
  filters: document.querySelector("#filters"),
  summary: document.querySelector("#summary"),
  message: document.querySelector("#message"),
  results: document.querySelector("#results"),
  exportButton: document.querySelector("#export-button"),
  importButton: document.querySelector("#import-button"),
  importInput: document.querySelector("#import-input"),
};

// Personal state is keyed by MusicBrainz ID and stored apart from catalog.json,
// so replacing the catalog never removes favorites, statuses, or backups.
const catalog = { albums: [], generatedAt: null };
const state = loadState();
let activeFilter = "all";
let messageTimer = null;

function loadState() {
  try {
    const saved = JSON.parse(localStorage.getItem(STATE_KEY));
    return { albums: saved?.albums ?? {} };
  } catch {
    return { albums: {} };
  }
}

function saveState() {
  try {
    localStorage.setItem(STATE_KEY, JSON.stringify(state));
  } catch (error) {
    showMessage(`Could not save changes: ${error.message}`);
  }
}

function normalize(text) {
  return String(text ?? "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase();
}

function tokensFor(query) {
  return normalize(query).split(/\s+/).filter(Boolean);
}

function matchesAll(text, tokens) {
  return tokens.every((token) => text.includes(token));
}

function indexAlbum(album) {
  const tracks = album.tracks.map((track) => ({
    ...track,
    searchText: normalize(track.title),
  }));
  const searchText = normalize(
    [
      album.title,
      album.year,
      album.date,
      ...album.artists,
      ...album.genres,
      ...album.personnel.flatMap((person) => [person.name, person.role]),
      ...tracks.map((track) => track.title),
    ].join(" ")
  );
  return { ...album, tracks, searchText };
}

function byDateThenTitle(a, b) {
  return (a.date || "9999").localeCompare(b.date || "9999") || a.title.localeCompare(b.title);
}

function timestamp(value) {
  return Date.parse(value) || 0;
}

function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function filterAlbums(tokens) {
  return catalog.albums.filter((album) => {
    const entry = state.albums[album.id] ?? {};
    const passesFilter =
      activeFilter === "all" ||
      (activeFilter === "favorites" && entry.favorite) ||
      (activeFilter === "to-listen" && entry.status === "to-listen") ||
      (activeFilter === "listened" && entry.status === "listened");
    return passesFilter && matchesAll(album.searchText, tokens);
  });
}

function render() {
  const tokens = tokensFor(elements.search.value);
  const albums = filterAlbums(tokens);
  const updated = catalog.generatedAt ? ` · catalog ${catalog.generatedAt.slice(0, 10)}` : "";
  elements.summary.textContent = `${albums.length} of ${catalog.albums.length} albums${updated}`;

  const nodes = albums.slice(0, RESULT_LIMIT).map((album) => renderAlbum(album, tokens));
  if (albums.length > RESULT_LIMIT) {
    nodes.push(el("p", "meta", `Showing first ${RESULT_LIMIT}. Refine your search to narrow results.`));
  }
  elements.results.replaceChildren(...nodes);
}

function renderAlbum(album, tokens) {
  const entry = state.albums[album.id] ?? {};
  const card = el("article", "album");
  const meta = [album.artists.join(", "), album.year, album.genres.join(", ")]
    .filter(Boolean)
    .join(" · ");
  card.append(el("h2", "", album.title), el("p", "meta", meta));

  const matchingTracks = tokens.length
    ? album.tracks.filter((track) => matchesAll(track.searchText, tokens)).slice(0, 5)
    : [];
  if (matchingTracks.length) {
    const list = el("ul", "track-matches");
    for (const track of matchingTracks) list.append(el("li", "", track.title));
    card.append(list);
  }

  card.append(renderControls(album.id, entry), renderDetails(album));
  return card;
}

function renderControls(albumId, entry) {
  const controls = el("div", "controls");
  controls.append(
    toggleButton(entry.favorite ? "★ Favorite" : "☆ Favorite", Boolean(entry.favorite), () =>
      updateAlbum(albumId, { favorite: !entry.favorite })
    )
  );
  for (const status of Object.keys(STATUS_LABELS)) {
    const active = entry.status === status;
    controls.append(
      toggleButton(STATUS_LABELS[status], active, () =>
        updateAlbum(albumId, { status: active ? null : status })
      )
    );
  }
  return controls;
}

function toggleButton(label, pressed, onClick) {
  const button = el("button", "", label);
  button.type = "button";
  button.setAttribute("aria-pressed", String(pressed));
  button.addEventListener("click", onClick);
  return button;
}

function renderDetails(album) {
  const details = el("details");
  details.append(el("summary", "", `Tracks and personnel (${album.tracks.length})`));

  const tracks = el("ol", "tracklist");
  for (const track of album.tracks) tracks.append(el("li", "", track.title));
  details.append(tracks);

  if (album.personnel.length) {
    const personnel = album.personnel.map((person) => `${person.name} (${person.role})`);
    details.append(el("p", "meta", personnel.join(", ")));
  }
  return details;
}

function updateAlbum(albumId, changes) {
  const previous = state.albums[albumId] ?? {};
  state.albums[albumId] = {
    favorite: false,
    status: null,
    ...previous,
    ...changes,
    updatedAt: new Date().toISOString(),
  };
  saveState();
  render();
}

function exportState() {
  const backup = {
    format: "jazz-archive-state",
    version: 1,
    exportedAt: new Date().toISOString(),
    albums: state.albums,
  };
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(backup, null, 2)], { type: "application/json" })
  );
  const link = el("a");
  link.href = url;
  link.download = `jazz-archive-backup-${new Date().toISOString().slice(0, 10)}.json`;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  showMessage("Backup downloaded.");
}

async function importState(file) {
  const payload = JSON.parse(await file.text());
  if (!payload || payload.format !== "jazz-archive-state" || typeof payload.albums !== "object" || payload.albums === null) {
    throw new Error("not a Jazz Archive backup");
  }

  // Newer entries win, so restoring an old backup cannot overwrite recent edits.
  let applied = 0;
  for (const [albumId, incoming] of Object.entries(payload.albums)) {
    if (!incoming || typeof incoming !== "object") continue;
    const existing = state.albums[albumId];
    if (!existing || timestamp(incoming.updatedAt) > timestamp(existing.updatedAt)) {
      state.albums[albumId] = incoming;
      applied += 1;
    }
  }
  saveState();
  render();
  showMessage(`Restored ${applied} entries.`);
}

function showMessage(text) {
  elements.message.textContent = text;
  clearTimeout(messageTimer);
  messageTimer = setTimeout(() => {
    elements.message.textContent = "";
  }, 4000);
}

async function loadCatalog() {
  const response = await fetch("catalog.json");
  if (!response.ok) throw new Error(`catalog request failed (${response.status})`);
  const data = await response.json();
  catalog.generatedAt = data.generatedAt;
  catalog.albums = data.albums.map(indexAlbum).sort(byDateThenTitle);
}

function wireEvents() {
  elements.search.addEventListener("input", render);

  elements.filters.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-filter]");
    if (!button) return;
    activeFilter = button.dataset.filter;
    for (const option of elements.filters.querySelectorAll("button")) {
      option.setAttribute("aria-pressed", String(option === button));
    }
    render();
  });

  elements.exportButton.addEventListener("click", exportState);
  elements.importButton.addEventListener("click", () => elements.importInput.click());
  elements.importInput.addEventListener("change", async () => {
    const file = elements.importInput.files[0];
    if (!file) return;
    try {
      await importState(file);
    } catch (error) {
      showMessage(`Restore failed: ${error.message}`);
    }
    elements.importInput.value = "";
  });
}

async function start() {
  wireEvents();
  try {
    await loadCatalog();
    render();
  } catch (error) {
    showMessage(`Could not load catalog: ${error.message}`);
  }
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("sw.js").catch((error) => {
      showMessage(`Offline support unavailable: ${error.message}`);
    });
  }
}

start();
