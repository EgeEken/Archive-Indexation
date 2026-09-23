let fileManagementRulesetId = null;

async function refreshFileManagementEditor() {
  const [profiles, rulesets] = await Promise.all([
    api("/api/file-management/profiles"),
    api("/api/file-management/rulesets"),
  ]);
  const profile = profiles.profiles[0];
  if (profile) {
    $("file-management-profile-name").value = profile.name;
    $("file-management-profile-codec").value = profile.codec;
    $("file-management-profile-container").value = profile.container;
    $("file-management-profile-settings").value = JSON.stringify(profile.settings || {}, null, 2);
  }
  const ruleset = rulesets.rulesets[0];
  if (ruleset) {
    fileManagementRulesetId = ruleset.id;
    $("file-management-ruleset-name").value = ruleset.name;
    $("file-management-rules").value = JSON.stringify(ruleset.rules || [], null, 2);
  }
}

$("file-management-button").addEventListener("click", async () => {
  try {
    await refreshFileManagementEditor();
    $("file-management-dialog").showModal();
  } catch (error) {
    showToast(`File-management editor failed: ${error.message}`);
  }
});
$("file-management-close").addEventListener("click", () => $("file-management-dialog").close());
$("file-management-profile-form").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    await api("/api/file-management/profiles", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        name: $("file-management-profile-name").value,
        codec: $("file-management-profile-codec").value,
        container: $("file-management-profile-container").value,
        settings: JSON.parse($("file-management-profile-settings").value || "{}"),
      }),
    });
    showToast("Compression profile saved.");
  } catch (error) {
    showToast(`Profile save failed: ${error.message}`);
  }
});
$("file-management-ruleset-form").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    const ruleset = await api("/api/file-management/rulesets", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        id: fileManagementRulesetId,
        name: $("file-management-ruleset-name").value,
        rules: JSON.parse($("file-management-rules").value || "[]"),
      }),
    });
    fileManagementRulesetId = ruleset.id;
    showToast("Ruleset saved.");
  } catch (error) {
    showToast(`Ruleset save failed: ${error.message}`);
  }
});
$("file-management-plan").addEventListener("click", async () => {
  try {
    const data = await api(`/api/file-management/plan${fileManagementRulesetId ? `?ruleset_id=${encodeURIComponent(fileManagementRulesetId)}` : ""}`);
    $("file-management-output").textContent = JSON.stringify(data, null, 2);
  } catch (error) {
    showToast(`Dry-run failed: ${error.message}`);
  }
});
