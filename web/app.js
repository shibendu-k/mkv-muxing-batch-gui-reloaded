const $ = (id) => document.getElementById(id);
const lines = (id) => $(id).value.split("\n").map((value) => value.trim()).filter(Boolean);
const jsonArray = (id) => JSON.parse($(id).value || "[]");

async function api(path, options) {
  const response = await fetch(path, options);
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
  return value;
}

function fillFields(path) {
  if (["info-source", "extract-source", "propedit-file", "mux-sources"].includes(document.activeElement?.id)) return;
  document.querySelectorAll("[id$='-source'], [id$='-file']").forEach((input) => {
    if (!input.value) input.value = path;
  });
  if (!$("mux-sources").value) $("mux-sources").value = path;
}

async function refreshStatus() {
  const status = await api("/api/status");
  $("tool-status").innerHTML = Object.entries(status.tools).map(([name, tool]) =>
    `<div><strong>${name}</strong>: ${tool.version ? tool.version : "missing"}</div>`
  ).join("");
}

async function refreshFiles() {
  const value = await api("/api/files");
  $("files").innerHTML = value.files.map((file) => {
    const isDir = file.type === "directory";
    return `<div class="file"><button class="file-name secondary" data-path="${file.path}" ${isDir ? 'disabled' : ''}>${file.relative}</button>
      ${isDir ? "" : `<button class="download" data-download="${file.path}">Download</button>`}</div>`;
  }).join("") || "<p>No files in /data.</p>";
  document.querySelectorAll(".file-name:not(:disabled)").forEach((button) =>
    button.addEventListener("click", () => fillFields(button.dataset.path))
  );
  document.querySelectorAll(".download").forEach((button) =>
    button.addEventListener("click", () => {
      window.open(`/api/download?path=${encodeURIComponent(button.dataset.download)}`);
    })
  );
}

async function refreshJobs() {
  const value = await api("/api/jobs");
  $("jobs").innerHTML = value.jobs.map((job) =>
    `<div class="job">
      <div class="job-header"><strong>${job.tool}</strong><span class="status-${job.status}">${job.status}</span></div>
      <code>${job.args.join(" ")}</code>
      ${job.stdout ? `<pre>${escapeHtml(job.stdout)}</pre>` : ""}
      ${job.stderr ? `<pre>${escapeHtml(job.stderr)}</pre>` : ""}
    </div>`
  ).join("") || "<p>No jobs.</p>";
}

function escapeHtml(value) {
  const node = document.createElement("div");
  node.textContent = value;
  return node.innerHTML;
}

async function startJob(path, body) {
  const value = await api(path, { method:"POST", headers:{ "Content-Type":"application/json" }, body:JSON.stringify(body) });
  await refreshJobs();
  return value;
}

document.querySelectorAll(".tab").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll(".tab,.tab-panel").forEach((item) => item.classList.remove("active"));
  button.classList.add("active");
  $(button.dataset.tab).classList.add("active");
}));

$("refresh-files").addEventListener("click", refreshFiles);
$("refresh-jobs").addEventListener("click", refreshJobs);
$("upload-button").addEventListener("click", async () => {
  const input = $("upload-file");
  for (const file of input.files) {
    const form = new FormData();
    form.append("file", file);
    await api("/api/upload", { method:"POST", body:form });
  }
  await refreshFiles();
});
$("mux-run").addEventListener("click", () =>
  startJob("/api/mux", {
    sources: lines("mux-sources"),
    output: $("mux-output").value,
    title: $("mux-title").value,
    attachments: lines("mux-attachments"),
    args: jsonArray("mux-args")
  }).catch(alert)
);
$("info-run").addEventListener("click", async () => {
  try {
    const value = await api(`/api/info?path=${encodeURIComponent($("info-source").value)}`);
    $("info-output").textContent = JSON.stringify(value, null, 2);
  } catch (error) { alert(error.message); }
});
$("extract-run").addEventListener("click", () =>
  startJob("/api/extract", {
    source: $("extract-source").value,
    mode: $("extract-mode").value,
    args: jsonArray("extract-args"),
    items: lines("extract-items").map((line) => {
      const [id, destination] = line.split(":");
      return { id: Number(id), destination };
    })
  }).catch(alert)
);
$("propedit-run").addEventListener("click", () =>
  startJob("/api/propedit", {
    file: $("propedit-file").value,
    edits: jsonArray("propedit-edits"),
    args: jsonArray("propedit-args")
  }).catch(alert)
);
$("raw-run").addEventListener("click", () =>
  startJob("/api/run", { tool:$("raw-tool").value, args:jsonArray("raw-args") }).catch(alert)
);

refreshStatus().catch(alert);
refreshFiles().catch(alert);
refreshJobs().catch(alert);
setInterval(refreshJobs, 3000);
