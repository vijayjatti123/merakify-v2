// Derive the stage from persisted output, never from a local click or planning's "done".
export function previewState(shot) {
  if (shot.still_frame_url) return { key: "ready", label: "Preview ready" };
  if (shot.still_frame_status === "generating") return { key: "generating", label: "Creating preview" };
  if (shot.still_frame_status === "failed") return { key: "failed", label: "Preview failed" };
  return { key: "waiting", label: "Waiting" };
}

export function previewSummary(result) {
  const shots = result?.shots || [];
  return {
    total: shots.length,
    ready: shots.filter(s => previewState(s).key === "ready").length,
    failed: shots.filter(s => previewState(s).key === "failed").length,
    busy: Boolean(result?.audio_assembly_pending || shots.some(s => previewState(s).key === "generating")),
  };
}
