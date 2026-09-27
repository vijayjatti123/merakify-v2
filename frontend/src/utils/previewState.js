// Derive the stage from persisted output, never from a local click or planning's "done".
export function hardPreviewMismatch(shot) {
  if (shot.still_frame_user_approved || shot.still_frame_verification?.approved !== false) return false;
  const checks = shot.still_frame_verification.visual_checks || {};
  return ["identity_wardrobe", "placement_support", "props_contact", "opening_state", "framing"]
    .some((key) => checks[key]?.status === "fail");
}

export function previewState(shot) {
  if (shot.still_frame_url && hardPreviewMismatch(shot)) return { key: "needs_correction", label: "Image needs correction" };
  if (shot.still_frame_url) return { key: "ready", label: "Preview ready" };
  if (shot.still_frame_status === "generating") return { key: "generating", label: shot.still_frame_candidate ? "Verifying preview" : "Creating preview" };
  if (shot.still_frame_status === "failed") return { key: "failed", label: shot.still_frame_error_kind === "verification" ? "Verification unavailable" : shot.still_frame_error_kind === "mismatch" ? "Preview needs adjustment" : "Preview failed" };
  return { key: "waiting", label: "Waiting" };
}

export function previewSummary(result) {
  const shots = result?.shots || [];
  return {
    total: shots.length,
    ready: shots.filter(s => previewState(s).key === "ready").length,
    needsCorrection: shots.filter(s => previewState(s).key === "needs_correction").length,
    failed: shots.filter(s => previewState(s).key === "failed").length,
    busy: Boolean(result?.audio_assembly_pending || result?.preview_preparation_pending || shots.some(s => previewState(s).key === "generating")),
  };
}
