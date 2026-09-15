import { Button, Alert } from "@mui/material";
import ActionProgress from "./ActionProgress";
import { friendlyMessage } from "../utils/presentation";
import { useEffect, useState } from "react";
import { enhanceShotFace } from "../api/client";

export default function FaceEnhancement({ jobId, shot, onRefresh }) {
  const [confirm, setConfirm] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const task = shot.face_enhancement;
  const busy = ["queued", "running"].includes(task?.status);
  if (!shot.has_dialogue || shot.video_provider !== "hedra") return null;
  const eligible = shot.video_url && shot.video_key && shot.video_status === "done" && !shot.video_source_changed && !shot.video_face_enhanced;
  async function start() {
    setSubmitting(true); setError("");
    try { await enhanceShotFace(jobId, shot); setConfirm(false); await onRefresh(); }
    catch (err) { setError(err.message); }
    finally { setSubmitting(false); }
  }
  return <div className="my-3 text-xs" aria-label={`Face enhancement for shot ${shot.shot_number}`}>
    {busy && <p role="status">Enhancing... {task.total_frames ? `${task.completed_frames}/${task.total_frames} frames` : "Queued"}. You can still view and approve the current unenhanced video.</p>}
    {busy && <ActionProgress label="Restoring facial detail…" value={task.total_frames ? 100 * task.completed_frames / task.total_frames : undefined} />}
    {submitting && <ActionProgress label="Starting face enhancement…" />}
    {task?.warning && <Alert severity="warning">{friendlyMessage(task.warning, "Face enhancement needs your review. Your original video remains available.")}</Alert>}
    {shot.video_face_enhanced && <p>Face enhancement applied · Experimental</p>}
    {eligible && !busy && !confirm && <Button type="button" className="card-action" onClick={() => setConfirm(true)}>Enhance Face</Button>}
    {eligible && !busy && confirm && <div className="p-3 border rounded-md" role="group" aria-label="Confirm face enhancement">
      <p>Sharpens facial detail using AI restoration. Takes approximately 10-12 minutes and roughly doubles this shot's generation cost. May cause subtle changes to facial appearance. Experimental — temporal flicker during playback has not been fully verified.</p>
      <p className="my-2">Actual time and cost depend on shot length, queue and provider limits. Your current video remains available while enhancement runs.</p>
      <Button type="button" className="card-action" disabled={submitting} onClick={start}>{submitting ? "Starting..." : "Start enhancement"}</Button>
      <Button type="button" className="card-action" disabled={submitting} onClick={() => setConfirm(false)}>Cancel</Button>
    </div>}
    {error && <Alert severity="error">{friendlyMessage(error, "Face enhancement could not start. Please try again.")}</Alert>}
  </div>;
}


// Signed URL refreshes during enhancement must not restart the current playback.
export function ShotVideo({ shot }) {
  const [source, setSource] = useState(shot.video_url);
  const identity = shot.video_key || shot.video_url;
  useEffect(() => { setSource(shot.video_url); }, [identity]);
  return <video controls preload="metadata" src={source} onError={() => setSource(shot.video_url)} className="my-3 w-full rounded-md" aria-label={`Generated video for shot ${shot.shot_number}`} />;
}
