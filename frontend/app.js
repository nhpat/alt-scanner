const API_BASE_URL = window.API_BASE_URL || "http://localhost:8000";
const POLL_INTERVAL_MS = 2000;
const IN_FLIGHT_STATUSES = new Set(["pending", "resizing", "resized", "captioning"]);

const uploadForm = document.getElementById("upload-form");
const fileInput = document.getElementById("file-input");
const uploadStatus = document.getElementById("upload-status");
const jobsList = document.getElementById("jobs-list");
const refreshBtn = document.getElementById("refresh-btn");
const clearJobsBtn = document.getElementById("clear-jobs-btn");
const dropzone = document.getElementById("dropzone");
const dropzoneEmpty = document.getElementById("dropzone-empty");
const dropzonePreview = document.getElementById("dropzone-preview");
const previewImg = document.getElementById("preview-img");
const previewName = document.getElementById("preview-name");
const lightbox = document.getElementById("lightbox");
const lightboxImg = document.getElementById("lightbox-img");
const lightboxCaption = document.getElementById("lightbox-caption");
const lightboxDownload = document.getElementById("lightbox-download");
const lightboxClose = document.getElementById("lightbox-close");

const scanForm = document.getElementById("scan-form");
const scanUrlInput = document.getElementById("scan-url-input");
const scanBtn = document.getElementById("scan-btn");
const scanStatus = document.getElementById("scan-status");
const scanResults = document.getElementById("scan-results");
const scanSummary = document.getElementById("scan-summary");
const scanCsvLink = document.getElementById("scan-csv-link");
const findingsList = document.getElementById("findings-list");
const scanHistoryList = document.getElementById("scan-history-list");
const scanHistoryRefreshBtn = document.getElementById("scan-history-refresh-btn");

const pollTimers = new Map();
let scanPollTimer = null;

function statusLabel(status) {
  return status.replace(/_/g, " ");
}

function jobCardHtml(job) {
  const thumbUrl = `${API_BASE_URL}/jobs/${job.id}/image/thumbnail`;
  const hasThumb = job.thumbnail_object_key || job.status === "done" || job.status === "caption_failed";
  const viewable = hasThumb && job.status !== "pending";

  return `
    <div class="job-card" data-job-id="${job.id}">
      <div class="job-thumb${viewable ? " job-thumb-clickable" : ""}" ${viewable ? 'data-action="view"' : ""}>
        ${hasThumb ? `<img src="${thumbUrl}" alt="${job.original_filename}" onerror="this.style.display='none'" />` : `<div class="job-thumb-placeholder"></div>`}
      </div>
      <div class="job-info">
        <div class="job-filename">${job.original_filename}</div>
        <div class="job-status status-${job.status}">${statusLabel(job.status)}</div>
        ${job.caption ? `<div class="job-caption">"${job.caption}"</div>` : ""}
        ${job.error_message ? `<div class="job-error">${job.error_message}</div>` : ""}
      </div>
    </div>
  `;
}

function renderJobs(jobs) {
  jobsList.innerHTML = jobs.map(jobCardHtml).join("") || "<p>No jobs yet — upload an image to get started.</p>";
}

async function fetchJob(jobId) {
  const res = await fetch(`${API_BASE_URL}/jobs/${jobId}`);
  if (!res.ok) throw new Error(`job ${jobId} fetch failed`);
  return res.json();
}

async function fetchJobs() {
  const res = await fetch(`${API_BASE_URL}/jobs`);
  if (!res.ok) throw new Error("jobs list fetch failed");
  return res.json();
}

function updateJobCard(job) {
  const card = jobsList.querySelector(`[data-job-id="${job.id}"]`);
  if (!card) return;
  card.outerHTML = jobCardHtml(job);
}

function pollJob(jobId) {
  if (pollTimers.has(jobId)) return;

  const tick = async () => {
    try {
      const job = await fetchJob(jobId);
      updateJobCard(job);
      if (!IN_FLIGHT_STATUSES.has(job.status)) {
        clearInterval(pollTimers.get(jobId));
        pollTimers.delete(jobId);
        return;
      }
    } catch (err) {
      console.error(err);
    }
  };

  const timer = setInterval(tick, POLL_INTERVAL_MS);
  pollTimers.set(jobId, timer);
}

async function loadJobs() {
  try {
    const jobs = await fetchJobs();
    renderJobs(jobs);
    jobs.forEach((job) => {
      if (IN_FLIGHT_STATUSES.has(job.status)) pollJob(job.id);
    });
  } catch (err) {
    jobsList.innerHTML = `<p class="job-error">Failed to load jobs: ${err.message}</p>`;
  }
}

function showPreview(file) {
  previewImg.src = URL.createObjectURL(file);
  previewName.textContent = file.name;
  dropzoneEmpty.hidden = true;
  dropzonePreview.hidden = false;
}

function clearPreview() {
  dropzoneEmpty.hidden = false;
  dropzonePreview.hidden = true;
  previewImg.src = "";
  previewName.textContent = "";
}

function setFile(file) {
  if (!file || !file.type.startsWith("image/")) return;
  const dt = new DataTransfer();
  dt.items.add(file);
  fileInput.files = dt.files;
  showPreview(file);
}

fileInput.addEventListener("change", () => {
  const file = fileInput.files[0];
  if (file) showPreview(file);
});

["dragover", "dragenter"].forEach((evt) => {
  dropzone.addEventListener(evt, (event) => {
    event.preventDefault();
    dropzone.classList.add("dragover");
  });
});

["dragleave", "dragend"].forEach((evt) => {
  dropzone.addEventListener(evt, () => dropzone.classList.remove("dragover"));
});

dropzone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropzone.classList.remove("dragover");
  const file = event.dataTransfer.files[0];
  setFile(file);
});

function openLightbox(job) {
  const variant = job.status === "done" || job.status === "caption_failed" ? "resized" : "thumbnail";
  lightboxImg.src = `${API_BASE_URL}/jobs/${job.id}/image/${variant}`;
  lightboxImg.alt = job.original_filename;
  lightboxCaption.textContent = job.caption || "";
  lightboxDownload.href = `${API_BASE_URL}/jobs/${job.id}/image/${variant}`;
  lightboxDownload.setAttribute("download", job.original_filename || "image");
  lightbox.hidden = false;
}

function closeLightbox() {
  lightbox.hidden = true;
  lightboxImg.src = "";
}

jobsList.addEventListener("click", async (event) => {
  const trigger = event.target.closest('[data-action="view"]');
  if (!trigger) return;
  const card = trigger.closest(".job-card");
  const jobId = card?.dataset.jobId;
  if (!jobId) return;
  try {
    const job = await fetchJob(jobId);
    openLightbox(job);
  } catch (err) {
    console.error(err);
  }
});

lightboxClose.addEventListener("click", closeLightbox);
lightbox.addEventListener("click", (event) => {
  if (event.target === lightbox) closeLightbox();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !lightbox.hidden) closeLightbox();
});

uploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = fileInput.files[0];
  if (!file) return;

  uploadStatus.textContent = "Uploading...";
  const formData = new FormData();
  formData.append("file", file);

  try {
    const res = await fetch(`${API_BASE_URL}/upload`, { method: "POST", body: formData });
    if (!res.ok) throw new Error(`upload failed (${res.status})`);
    const { job_id } = await res.json();
    uploadStatus.textContent = `Queued job ${job_id}`;
    fileInput.value = "";
    clearPreview();
    await loadJobs();
    pollJob(job_id);
  } catch (err) {
    uploadStatus.textContent = `Error: ${err.message}`;
  }
});

refreshBtn.addEventListener("click", loadJobs);

clearJobsBtn.addEventListener("click", async () => {
  if (!confirm("Delete all jobs and their stored images? This can't be undone.")) return;
  clearJobsBtn.disabled = true;
  try {
    const res = await fetch(`${API_BASE_URL}/jobs`, { method: "DELETE" });
    if (!res.ok) throw new Error(`clear failed (${res.status})`);
    pollTimers.forEach((timer) => clearInterval(timer));
    pollTimers.clear();
    await loadJobs();
  } catch (err) {
    jobsList.innerHTML = `<p class="job-error">Failed to clear jobs: ${err.message}</p>`;
  } finally {
    clearJobsBtn.disabled = false;
  }
});

loadJobs();

function findingRowHtml(finding) {
  const badAlt = finding.current_alt ? `"${finding.current_alt}"` : "<em>missing</em>";
  const suggested = finding.status === "done"
    ? `"${finding.suggested_alt}"`
    : finding.status === "failed"
      ? `<span class="job-error">${finding.error_message || "failed"}</span>`
      : "generating&hellip;";

  return `
    <div class="finding-row">
      <img class="finding-thumb" src="${finding.image_url}" alt="" loading="lazy" onerror="this.style.visibility='hidden'" />
      <div class="finding-info">
        <div class="finding-current">Current: ${badAlt}</div>
        <div class="finding-suggested">Suggested: ${suggested}</div>
      </div>
    </div>
  `;
}

async function fetchScan(scanId) {
  const res = await fetch(`${API_BASE_URL}/scans/${scanId}`);
  if (!res.ok) throw new Error(`scan ${scanId} fetch failed`);
  return res.json();
}

async function fetchFindings(scanId) {
  const res = await fetch(`${API_BASE_URL}/scans/${scanId}/findings`);
  if (!res.ok) throw new Error(`findings for ${scanId} fetch failed`);
  return res.json();
}

function renderScanSummary(scan) {
  if (scan.status === "scanning") {
    scanStatus.textContent =
      `Crawling... ${scan.pages_scanned} page${scan.pages_scanned === 1 ? "" : "s"} scanned, ` +
      `${scan.images_found} image${scan.images_found === 1 ? "" : "s"} flagged so far.`;
  } else if (scan.status === "failed") {
    scanStatus.textContent = `Scan failed: ${scan.error_message || "unknown error"}`;
  } else {
    scanStatus.textContent = `Scan complete — ${scan.pages_scanned} page${scan.pages_scanned === 1 ? "" : "s"} scanned.`;
  }

  if (scan.images_found > 0) {
    scanSummary.hidden = false;
    scanCsvLink.hidden = false;
    scanSummary.textContent =
      `${scan.images_found} image${scan.images_found === 1 ? "" : "s"} flagged — ` +
      `${scan.findings_done} done, ${scan.findings_pending} pending, ${scan.findings_failed} failed`;
  } else {
    scanSummary.hidden = true;
    scanCsvLink.hidden = true;
  }
}

async function pollScan(scanId) {
  if (scanPollTimer) clearInterval(scanPollTimer);
  scanCsvLink.href = `${API_BASE_URL}/scans/${scanId}/report.csv`;
  scanResults.hidden = false;

  const tick = async () => {
    try {
      const [scan, findings] = await Promise.all([fetchScan(scanId), fetchFindings(scanId)]);
      renderScanSummary(scan);
      findingsList.innerHTML = findings.map(findingRowHtml).join("");
      if (scan.status !== "scanning") {
        clearInterval(scanPollTimer);
        scanPollTimer = null;
        loadScanHistory();
      }
    } catch (err) {
      console.error(err);
    }
  };

  await tick();
  scanPollTimer = setInterval(tick, POLL_INTERVAL_MS);
}

function scanStatusLabel(status) {
  return status === "scanning" ? "scanning" : status;
}

function scanHistoryRowHtml(scan) {
  const badgeStatus = scan.status === "scanning" ? "pending" : scan.status;
  return `
    <div class="scan-history-row" data-scan-id="${scan.id}">
      <div class="scan-history-url">${scan.root_url}</div>
      <div class="scan-history-meta">
        <span class="job-status status-${badgeStatus}">${scanStatusLabel(scan.status)}</span>
        <span>${scan.pages_scanned} page${scan.pages_scanned === 1 ? "" : "s"}, ${scan.images_found} flagged</span>
      </div>
    </div>
  `;
}

async function loadScanHistory() {
  try {
    const res = await fetch(`${API_BASE_URL}/scans`);
    if (!res.ok) throw new Error("scan history fetch failed");
    const scans = await res.json();
    scanHistoryList.innerHTML = scans.map(scanHistoryRowHtml).join("") || "<p>No scans yet.</p>";
  } catch (err) {
    scanHistoryList.innerHTML = `<p class="job-error">Failed to load scan history: ${err.message}</p>`;
  }
}

scanHistoryList.addEventListener("click", (event) => {
  const row = event.target.closest(".scan-history-row");
  if (!row) return;
  pollScan(row.dataset.scanId);
});

scanHistoryRefreshBtn.addEventListener("click", loadScanHistory);

scanForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const url = scanUrlInput.value.trim();
  if (!url) return;

  scanBtn.disabled = true;
  scanStatus.textContent = "Starting crawl...";
  scanResults.hidden = true;
  findingsList.innerHTML = "";

  try {
    const res = await fetch(`${API_BASE_URL}/scans`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `scan failed (${res.status})`);
    }
    const { scan_id } = await res.json();
    await loadScanHistory();
    await pollScan(scan_id);
  } catch (err) {
    scanStatus.textContent = `Error: ${err.message}`;
  } finally {
    scanBtn.disabled = false;
  }
});

loadScanHistory();
