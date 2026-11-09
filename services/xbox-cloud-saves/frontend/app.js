// Xbox Cloud Saves — demo frontend.
//
// Talks to the FastAPI backend. This page is served from the backend's
// /ui mount, so the API is same-origin; the API base is derived from
// the page URL so the app works both at the root (/ui/) and under a
// hosting path prefix (e.g. /xbox/ui/ behind the deployment gateway).
// If the file is opened directly from disk, it falls back to a
// backend on http://127.0.0.1:8000.

const API = (() => {
  if (!location.protocol.startsWith("http")) return "http://127.0.0.1:8000";
  const i = location.pathname.lastIndexOf("/ui");
  return i >= 0 ? location.pathname.slice(0, i) : "";
})();

const state = {
  authorization: localStorage.getItem("xsave.authorization") || null,
  xuid: localStorage.getItem("xsave.xuid") || null,
  gamertag: localStorage.getItem("xsave.gamertag") || null,
};

const $ = (id) => document.getElementById(id);

function setStatus(msg, isError = false) {
  const el = $("status");
  el.textContent = msg;
  el.style.color = isError ? "#e81123" : "#107c10";
}

function authHeaders() {
  return state.authorization ? { "Authorization": state.authorization } : {};
}

async function api(path, options = {}) {
  const res = await fetch(API + path, {
    ...options,
    headers: { ...(options.headers || {}), ...authHeaders() },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).error || detail; } catch (e) { /* keep statusText */ }
    throw new Error(`${res.status} — ${detail}`);
  }
  return res;
}

// ---------------------------------------------------------------- sign-in

async function signIn() {
  const gamertag = $("gamertag").value.trim();
  if (!gamertag) { alert("Enter a gamertag first."); return; }
  try {
    const res = await fetch(API + "/auth/dev-login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gamertag }),
    });
    if (!res.ok) throw new Error("login failed");
    const data = await res.json();
    state.authorization = data.authorization;
    state.xuid = data.xuid;
    state.gamertag = data.gamertag;
    localStorage.setItem("xsave.authorization", data.authorization);
    localStorage.setItem("xsave.xuid", data.xuid);
    localStorage.setItem("xsave.gamertag", data.gamertag);
    renderIdentity();
    await refreshList();
  } catch (err) {
    alert("Sign-in failed: " + err.message + " (is the backend running?)");
  }
}

function signOut() {
  state.authorization = state.xuid = state.gamertag = null;
  localStorage.removeItem("xsave.authorization");
  localStorage.removeItem("xsave.xuid");
  localStorage.removeItem("xsave.gamertag");
  renderIdentity();
}

function renderIdentity() {
  const signedIn = !!state.authorization;
  $("identity").textContent = signedIn
    ? `Signed in as ${state.gamertag} — XUID ${state.xuid} (DEMO token — local test key, not Microsoft)`
    : "Not signed in.";
  $("signout-btn").classList.toggle("hidden", !signedIn);
  $("saves-panel").classList.toggle("hidden", !signedIn);
}

// ---------------------------------------------------------------- saves

async function refreshList() {
  if (!state.authorization) return;
  try {
    const res = await api("/saves");
    const data = await res.json();
    const body = $("saves-body");
    body.innerHTML = "";
    if (!data.saves.length) {
      body.innerHTML = '<tr><td colspan="5" class="muted">No saves yet — upload one below.</td></tr>';
      return;
    }
    for (const s of data.saves) {
      const tr = document.createElement("tr");
      const updated = new Date(s.timestamp * 1000).toLocaleString();
      tr.innerHTML = `
        <td></td><td>${s.saveVersion}</td><td>${s.size} B</td><td>${updated}</td>
        <td>
          <button data-act="download">Download</button>
          <button data-act="delete">Delete</button>
        </td>`;
      tr.children[0].textContent = s.name;
      tr.querySelector('[data-act="download"]').onclick = () => downloadSave(s.name);
      tr.querySelector('[data-act="delete"]').onclick = () => deleteSave(s.name);
      body.appendChild(tr);
    }
    setStatus(`Loaded ${data.saves.length} save(s) for XUID ${data.xuid}.`);
  } catch (err) {
    setStatus("Could not load saves: " + err.message, true);
  }
}

async function uploadSave() {
  const name = $("save-name").value.trim();
  const file = $("save-file").files[0];
  if (!name) { alert("Enter a save name."); return; }
  if (!file) { alert("Choose a file to upload."); return; }
  try {
    // Step 1 (production flow): ask the API for an upload URL.
    const res = await api("/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const { uploadUrl } = await res.json();
    // Step 2: send the bytes directly to that URL (S3 in production).
    const put = await fetch(uploadUrl, { method: "PUT", body: file });
    if (!put.ok) throw new Error("transfer failed (" + put.status + ")");
    const meta = await put.json();
    setStatus(`Uploaded "${name}" — now at version ${meta.saveVersion}, sha256 ${meta.sha256.slice(0, 12)}…`);
    await refreshList();
  } catch (err) {
    setStatus("Upload failed: " + err.message, true);
  }
}

async function downloadSave(name) {
  try {
    // Step 1: ask the API for a download URL.
    const res = await api("/saves/" + encodeURIComponent(name) + "/download-url");
    const { downloadUrl } = await res.json();
    // Step 2: fetch the bytes from that URL and save them locally.
    const fileRes = await fetch(downloadUrl);
    if (!fileRes.ok) throw new Error("transfer failed (" + fileRes.status + ")");
    const blob = await fileRes.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    URL.revokeObjectURL(a.href);
    setStatus(`Downloaded "${name}".`);
  } catch (err) {
    setStatus("Download failed: " + err.message, true);
  }
}

async function deleteSave(name) {
  if (!confirm(`Delete save "${name}"?`)) return;
  try {
    await api("/saves/" + encodeURIComponent(name), { method: "DELETE" });
    setStatus(`Deleted "${name}".`);
    await refreshList();
  } catch (err) {
    setStatus("Delete failed: " + err.message, true);
  }
}

// ---------------------------------------------------------------- wiring

$("signin-btn").onclick = signIn;
$("signout-btn").onclick = signOut;
$("refresh-btn").onclick = refreshList;
$("upload-btn").onclick = uploadSave;
if (state.gamertag) $("gamertag").value = state.gamertag;
renderIdentity();
if (state.authorization) refreshList();
