const statusEl = document.getElementById("status");
const integrationEl = document.getElementById("integration");

chrome.runtime.sendMessage({ type: "status" }, (response) => {
  if (!response || response.type !== "status_ok") {
    statusEl.textContent = response ? `Error: ${response.message}` : "No response";
    integrationEl.textContent = "MagnetoClip not running";
    return;
  }
  const active = response.active || 0;
  const completed = response.completed || 0;
  statusEl.textContent = `Active: ${active}  Completed: ${completed}`;
  integrationEl.textContent = response.integration_enabled
    ? "Integration: Enabled"
    : "Integration: Disabled";
});
