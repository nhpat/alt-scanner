const API_BASE_URL = window.API_BASE_URL || "http://localhost:8000";

const fullHistoryList = document.getElementById("full-history-list");
const historyRefreshBtn = document.getElementById("history-refresh-btn");
const clearScansBtn = document.getElementById("clear-scans-btn");
const historyDetail = document.getElementById("history-detail");
const historyDetailTitle = document.getElementById("history-detail-title");
const historyDetailCsv = document.getElementById("history-detail-csv");
const historyDetailFindings = document.getElementById("history-detail-findings");

function scanStatusLabel(status) {
  return status === "scanning" ? "scanning" : status;
}

function scanRowHtml(scan) {
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

async function loadHistory() {
  try {
    const res = await fetch(`${API_BASE_URL}/scans`);
    if (!res.ok) throw new Error("scan history fetch failed");
    const scans = await res.json();
    fullHistoryList.innerHTML = scans.map(scanRowHtml).join("") || "<p>No scans yet.</p>";
  } catch (err) {
    fullHistoryList.innerHTML = `<p class="job-error">Failed to load scan history: ${err.message}</p>`;
  }
}

async function showScanDetail(scanId) {
  try {
    const [scanRes, findingsRes] = await Promise.all([
      fetch(`${API_BASE_URL}/scans/${scanId}`),
      fetch(`${API_BASE_URL}/scans/${scanId}/findings`),
    ]);
    if (!scanRes.ok || !findingsRes.ok) throw new Error("failed to load scan detail");
    const scan = await scanRes.json();
    const findings = await findingsRes.json();

    historyDetailTitle.textContent = scan.root_url;
    historyDetailCsv.href = `${API_BASE_URL}/scans/${scanId}/report.csv`;
    historyDetailFindings.innerHTML = findings.map(findingRowHtml).join("") || "<p>No images were flagged on this scan.</p>";
    historyDetail.hidden = false;
    historyDetail.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    historyDetailFindings.innerHTML = `<p class="job-error">${err.message}</p>`;
    historyDetail.hidden = false;
  }
}

fullHistoryList.addEventListener("click", (event) => {
  const row = event.target.closest(".scan-history-row");
  if (!row) return;
  showScanDetail(row.dataset.scanId);
});

historyRefreshBtn.addEventListener("click", loadHistory);

clearScansBtn.addEventListener("click", async () => {
  if (!confirm("Delete all scan history? This can't be undone.")) return;
  clearScansBtn.disabled = true;
  try {
    const res = await fetch(`${API_BASE_URL}/scans`, { method: "DELETE" });
    if (!res.ok) throw new Error(`clear failed (${res.status})`);
    historyDetail.hidden = true;
    await loadHistory();
  } catch (err) {
    fullHistoryList.innerHTML = `<p class="job-error">Failed to clear history: ${err.message}</p>`;
  } finally {
    clearScansBtn.disabled = false;
  }
});

loadHistory();
