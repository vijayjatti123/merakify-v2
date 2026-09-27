import { Button, Alert } from "@mui/material";
import ActionProgress from "./ActionProgress";
import { friendlyMessage } from "../utils/presentation";
import { useEffect, useRef, useState } from "react";
import { enhanceShotFace, saveVideoEditRange } from "../api/client";

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
export function ShotVideo({ shot, jobId, onRefresh }) {
  const [source, setSource] = useState(shot.video_url);
  const [duration, setDuration] = useState(0);
  const [start, setStart] = useState(shot.video_edit_range?.start_sec ?? 0);
  const [end, setEnd] = useState(shot.video_edit_range?.end_sec ?? 0);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const player = useRef(null);
  const identity = shot.video_key || shot.video_url;
  useEffect(() => { setSource(shot.video_url); }, [identity]);
  useEffect(() => {
    setStart(shot.video_edit_range?.start_sec ?? 0);
    setEnd(shot.video_edit_range?.end_sec ?? duration);
  }, [identity, shot.video_edit_range?.start_sec, shot.video_edit_range?.end_sec, duration]);
  const canSelect = jobId && shot.video_key && shot.video_status === "done" && !shot.video_source_changed &&
    !shot.has_dialogue && [undefined, null, "none"].includes(shot.speech_mode);
  async function save(range) {
    setSaving(true); setError("");
    try { await saveVideoEditRange(jobId, shot, range); await onRefresh(); }
    catch (failure) { setError(failure.message); }
    finally { setSaving(false); }
  }
  const fmt = (value) => `${Math.max(0, value).toFixed(1)}s`;
  return <div>
    <video ref={player} controls preload="metadata" src={source} onError={() => setSource(shot.video_url)}
      onLoadedMetadata={(event) => setDuration(event.currentTarget.duration)}
      className="my-3 w-full rounded-md" aria-label={`Generated video for shot ${shot.shot_number}`} />
    {canSelect && Number.isFinite(duration) && duration >= 0.5 && <details className="text-sm my-2" data-testid={`footage-selection-${shot.shot_number}`}>
      <summary className="cursor-pointer" style={{ color: "var(--mui-palette-primary-main)" }}>Keep the best part of this clip (optional)</summary>
      <p className="my-2">Play the video and pause at the moments you want to keep. The original clip stays saved.</p>
      <div className="flex flex-wrap items-center gap-2 my-2">
        <Button size="small" variant="outlined" onClick={() => setStart(Math.max(0, Math.min(player.current?.currentTime ?? 0, end - 0.5)))}>Start here</Button>
        <span>From {fmt(start)}</span>
        <Button size="small" variant="outlined" onClick={() => setEnd(Math.max(player.current?.currentTime ?? duration, start + 0.5))}>End here</Button>
        <span>To {fmt(end)}</span>
      </div>
      <Button size="small" variant="contained" disabled={saving || start < 0 || end > duration + 0.05 || end - start < 0.5}
        onClick={() => save({ start_sec: start, end_sec: end })}>{saving ? "Saving…" : "Use this part in final video"}</Button>
      {shot.video_edit_range && <Button size="small" disabled={saving} onClick={() => save(null)}>Use whole clip</Button>}
      {error && <Alert severity="error" sx={{ mt: 1 }}>{friendlyMessage(error, "Could not save the selected footage.")}</Alert>}
    </details>}
  </div>;
}
