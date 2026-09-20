import { AD_TYPES } from "../components/CommercialIntake";
import PreviewLightbox from "../components/PreviewLightbox";
import DirectorPlanEditor, { editablePlan } from "../components/DirectorPlanEditor";
import { AlertTriangle, Check, Clapperboard, Clock3, Loader2, Pencil, RefreshCw, Save, X } from "lucide-react";
import { useEffect, useState } from "react";
import { Alert, AlertTitle, Button, Card, Stepper, Step, StepLabel, Box, Stack, Typography, Chip } from "@mui/material";
import StoryboardDraft from "../components/StoryboardDraft";
import ActionProgress from "../components/ActionProgress";
import ShotImageActions from "../components/ShotImageActions";
import { AdDirectionPlan, ShotDirectionPlan } from "../components/AdDirectionPlan";
import ShotPreviewImage from "../components/ShotPreviewImage";
import { friendlyMessage, progressMessage, videoReviewGuidance, videoReviewWarnings } from "../utils/presentation";
import { retryFailedJob, retryShotPreview, retryPreviewPreparation } from "../api/client";
import { previewState, previewSummary } from "../utils/previewState";
import { approveJob, reviseJob, streamJob, getJob, generateShotVideo, regenerateShotVideo, assembleFinalVideo } from "../api/client";
import FaceEnhancement, { ShotVideo } from "../components/FaceEnhancement";
import { CAMERA_VOCABULARY } from "../utils/cameraVocabulary";

const COLORS = {
  bg: "var(--mui-palette-background-default)",
  panel: "var(--mui-palette-background-paper)",
  field: "var(--mui-palette-action-hover)",
  border: "var(--mui-palette-divider)",
  text: "var(--mui-palette-text-primary)",
  muted: "var(--mui-palette-text-secondary)",
  marigold: "var(--mui-palette-primary-main)",
  green: "var(--mui-palette-success-main)",
  red: "var(--mui-palette-error-main)",
};

const videoReady = (shot) => Boolean(shot.video_url && shot.video_status === "done" && !shot.video_source_changed);
const lockedAudioReview = (shot) => shot.video_status === "review_required" && shot.video_audio_lock?.policy === "approved-dialogue-plus-silence-v1";

function shotOutputStatus(shot, audioStatus) {
  if (["submitting", "processing"].includes(shot.video_status)) return ["generating", "Generating video"];
  if (shot.video_status === "submission_unknown") return ["pending", "Checking video request"];
  if (shot.video_source_changed) return ["pending", "Video needs regeneration"];
  if (lockedAudioReview(shot)) return ["generating", "Finishing saved video"];
  if (shot.video_status === "review_required") return ["error", "Video needs a correction"];
  if (["error", "failed"].includes(shot.video_status)) return ["error", "Video generation failed"];
  if (videoReady(shot)) return ["done", "Video ready"];
  if (shot.still_frame_status === "generating") return ["generating", shot.still_frame_candidate ? "Verifying preview" : "Creating preview"];
  if (shot.still_frame_status === "failed" && !shot.still_frame_url) return ["error", shot.still_frame_error_kind === "verification" ? "Verification unavailable" : shot.still_frame_error_kind === "mismatch" ? "Preview needs adjustment" : "Preview failed"];
  if (shot.has_dialogue && audioStatus === "error") return ["error", "Audio preparation failed"];
  if (shot.still_frame_url) return ["done", "Preview ready"];
  return ["pending", "Waiting"];
}

function FinalVideo({ data, shots, busy, onAssemble }) {
  const missing = shots.filter((shot) => !videoReady(shot)).map((shot) => shot.shot_number);
  const readyCount = shots.length - missing.length;
  const needsRecovery = shots.some((shot) => !videoReady(shot) && (shot.video_source_changed || ["error", "failed", "review_required"].includes(shot.video_status) || shot.still_frame_status === "failed"));
  return (
    <Card component="section" className="generation-stage generation-placeholder generation-placeholder--final" aria-label="Final video" aria-live="polite">
      {busy ? <Loader2 size={30} className="animate-spin" /> : <Clapperboard size={32} />}
      <p className="eyebrow">{missing.length ? "Video clips" : "Your finished video"}</p>
      <h2>{busy ? "Putting it all together…" : data?.url ? "Your video is ready" : needsRecovery ? "Some shots need another try" : missing.length ? "Turn your previews into video clips" : "Bring your shots together"}</h2>
      {busy && <ActionProgress label="Combining your shots, sound and transitions…" />}
      {data?.url && <video controls preload="metadata" src={data.url} className="w-full rounded-md my-3" style={{ maxHeight: "60vh" }} aria-label="Final assembled video" />}
      {data?.url && <a href={data.url} target="_blank" rel="noreferrer" className="underline">Open final video</a>}
      {data?.stale && <p className="audio-warning">Your shots have changed. Combine them again to update the final video.</p>}
      {data?.error && <Alert severity="error">{friendlyMessage(data.error, "Your final video could not be finished. Please try again.")}</Alert>}
      <p role="status" style={{ fontWeight: 600 }}>{readyCount} of {shots.length} videos ready</p>
      {missing.length > 0 && <p>{needsRecovery
        ? "Use Retry preview for a missing image, or Regenerate for a failed video. Then generate any remaining clips."
        : "Preview your shots, then click 'Generate video' on each one to create its clip. Once every clip is ready, combine them here."}</p>}
      {missing.length === 0 && shots.length > 0 && !data?.url && <p>All your clips are ready. Click 'Create final video' to combine them.</p>}
      <p>Keeps your shots’ sound and planned transitions.</p>
    </Card>
  );
}

const isCompilerTimeout = (text) => /Shot Prompt Compiler.*(?:deadline exceeded|timed?\s*out)/i.test(text || "");

export default function JobView({ jobId, onReset, initialJob = null, onRetry }) {
  const [expandedPreview, setExpandedPreview] = useState(null);
  const [trace, setTrace] = useState([]);
  const [final, setFinal] = useState(initialJob);
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState("");
  const [error, setError] = useState("");
  const [approving, setApproving] = useState(false);
  const [editingShot, setEditingShot] = useState(null);
  const [editValues, setEditValues] = useState({ description: "", dialogue_text: "" });
  const [savingShot, setSavingShot] = useState(null);
  const [regeneratingShot, setRegeneratingShot] = useState(null);
  const [shotStatuses, setShotStatuses] = useState({});
  const [assembling, setAssembling] = useState(false);
  const [streamCycle, setStreamCycle] = useState(0);
  const [videoSubmitting, setVideoSubmitting] = useState(null);
  const [statusRefreshing, setStatusRefreshing] = useState(null);
  const [videoHints, setVideoHints] = useState({});
  const [previewSubmitting, setPreviewSubmitting] = useState(null);
  const videoBusy = !final || final?.result?.audio_assembly_pending || final?.result?.preview_preparation_pending || final?.status === "running" || final?.result?.shots?.some((shot) => (shot.still_frame_status === "generating" || ["submitting", "processing"].includes(shot.video_status) || lockedAudioReview(shot) || ["queued", "running"].includes(shot.face_enhancement?.status))) || final?.result?.final_video?.status === "running";

  useEffect(() => {
    if (!videoBusy && !final?.result?.shots?.some(s => s.preview_replacement?.status === "working")) return;
    let stopped = false;
    let timer;
    async function refresh() {
      try {
        const job = await getJob(jobId);
        if (!stopped) setFinal(job);
      } catch (err) {
        if (!stopped) setError(err.message);
      }
      if (!stopped) timer = setTimeout(refresh, 5000);
    }
    timer = setTimeout(refresh, 1000);
    return () => { stopped = true; clearTimeout(timer); };
  }, [jobId, videoBusy, final?.result?.shots?.some(s => s.preview_replacement?.status === "working")]);

  async function handleVideo(shotNumber) {
    setVideoSubmitting(shotNumber);
    setError("");
    try {
      await generateShotVideo(jobId, shotNumber);
    } catch (err) {
      setError(err.message);
    } finally {
      try { setFinal(await getJob(jobId)); } catch (err) { setError(err.message); }
      setVideoSubmitting(null);
    }
  }

  async function refreshVideoStatus(shot) {
    setStatusRefreshing(shot.shot_number);
    setError("");
    try {
      setFinal(await getJob(jobId));
    } catch (err) {
      setError(err.message);
    } finally {
      setStatusRefreshing(null);
    }
  }

  useEffect(() => {
    const source = streamJob(jobId, {
      onEvent: (event) => {
        setTrace((current) => [...current, event]);
        if (event.agent_key === "shot_status" && event.shot_number) {
          setShotStatuses((current) => ({ ...current, [event.shot_number]: event.status }));
          setFinal((current) => {
            if (!current?.result?.shots) return current;
            const eventFields = Object.fromEntries(
              ["status", "error_message", "dialogue_audio_url", "dialogue_audio_provider", "dialogue_voice_id"]
                .filter((field) => event[field] !== undefined)
                .map((field) => [field, event[field]]),
            );
            return {
              ...current,
              result: {
                ...current.result,
                shots: current.result.shots.map((shot) => (
                  shot.shot_number === event.shot_number ? { ...shot, ...eventFields } : shot
                )),
              },
            };
          });
        }
      },
      onFinal: (payload) => {
        setFinal(payload);
        if (payload.result?.shots) {
          setShotStatuses(Object.fromEntries(payload.result.shots.map((shot) => [shot.shot_number, shot.status])));
        }
      },
      onError: () => setError("The live progress connection was interrupted."),
    });
    return () => source.close();
  }, [jobId, streamCycle]);

  const done = final?.status === "done";
  const errored = final?.status === "error";
  const result = final?.result;
  const shots = result?.shots || [];
  const draft = result?.planning_draft;
  const editingSource = shots.find(shot => shot.shot_number === editingShot);
  const hasUnsavedEdit = Boolean(editingSource && (
    JSON.stringify(editValues) !== JSON.stringify(editablePlan(editingSource))
  ));
  const editBlocksApproval = hasUnsavedEdit || savingShot !== null;
  const approved = Boolean(result?.generation_approved);
  const previews = previewSummary(result);
  const allVideosReady = shots.length > 0 && shots.every(videoReady);
  const latestTrace = trace[trace.length - 1];
  const compilerTimeout = errored && isCompilerTimeout(final?.error_message);
  const canResumePreviews = approved && shots.length > 0 && !previews.busy && (!shots.some(s => s.video_url) || result?.plan_edited_shots?.length) && (!shots.some(s => s.still_frame_url) || result?.video_prompt_error || previews.failed > 0) &&
    shots.every(s => !s.has_dialogue || (s.status === "done" && s.dialogue_audio_url)) && (errored || result?.assembly?.provisional || previews.failed > 0);
  const progressNote = compilerTimeout ? "This shot is taking longer than expected." : done ? "Your plan is ready to review." : progressMessage(latestTrace);

  async function handleTimeoutRetry() {
    setRetrying(true); setRetryError("");
    try {
      if (canResumePreviews) { setFinal(await retryPreviewPreparation(jobId)); setStreamCycle(n => n + 1); }
      else onRetry(await retryFailedJob(jobId));
    }
    catch (err) { setRetryError(err.message); }
    finally { setRetrying(false); }
  }
  const castingWarnings = [...new Set([
    ...trace.filter((event) => event.agent_key === "casting_warning").map((event) => event.note),
    ...(result?.continuity?.characters || []).map((character) => character.casting_warning).filter(Boolean),
  ])];

  const audioReady = shots.length > 0 && shots.every((shot) => (shotStatuses[shot.shot_number] || shot.status) === "done") && !result?.audio_assembly_pending && !result?.assembly?.provisional;
  const currentStep = result?.final_video?.url && !result.final_video.stale ? 4 : allVideosReady ? 3 :
    approved && audioReady && previews.ready > 0 && !previews.busy ? 2 : approved ? 1 : 0;

  async function handlePreviewRetry(shot) {
    setPreviewSubmitting(shot.shot_number); setError("");
    try { await retryShotPreview(jobId, shot); }
    catch (err) { setError(err.message); }
    finally {
      try { setFinal(await getJob(jobId)); } catch (err) { setError(err.message); }
      setPreviewSubmitting(null);
    }
  }

  async function handleAssemble() {
    setAssembling(true);
    setError("");
    try {
      await assembleFinalVideo(jobId);
    } catch (err) {
      setError(err.message);
    } finally {
      try { setFinal(await getJob(jobId)); } catch (err) { setError(err.message); }
      setAssembling(false);
    }
  }

  function beginEdit(shot) {
    setEditingShot(shot.shot_number);
    setEditValues(editablePlan(shot));
    setError("");
  }

  async function saveEdit(shotNumber) {
    setSavingShot(shotNumber);
    setError("");
    try {
      const job = await reviseJob(jobId, [{ shot_number: shotNumber, ...editValues }], result.plan_revision || 0);
      setShotStatuses({});
      setFinal({ status: job.status, error_message: job.error_message, result: job.result });
      setEditingShot(null);
    } catch (saveError) {
      setError(saveError.message);
    } finally {
      setSavingShot(null);
    }
  }

  async function handleApprove() {
    if (editBlocksApproval) return;
    setApproving(true);
    setError("");
    try {
      const job = await approveJob(jobId);
      setTrace([]);
      setShotStatuses(Object.fromEntries(job.result.shots.map((shot) => [shot.shot_number, shot.status])));
      setFinal({ status: job.status, error_message: job.error_message, result: job.result });
      setStreamCycle((current) => current + 1);
    } catch (approveError) {
      setError(approveError.message);
    } finally {
      setApproving(false);
    }
  }

  async function handleRegenerate(shotNumber) {
    const hint = (videoHints[shotNumber] || "").trim();
    setRegeneratingShot(shotNumber);
    setError("");
    try {
      const shot = shots.find((item) => item.shot_number === shotNumber);
      await regenerateShotVideo(jobId, shot, hint);
      setVideoHints((current) => ({ ...current, [shotNumber]: "" }));
      setFinal(await getJob(jobId));
    } catch (regenerateError) {
      setError(regenerateError.message);
    } finally {
      try { setFinal(await getJob(jobId)); } catch (err) { setError(err.message); }
      setRegeneratingShot(null);
    }
  }

  return (
    <>
      <section className="generation-area storyboard-progress">
        <Box sx={{ mb: 3 }}><Stepper activeStep={currentStep} alternativeLabel>
          {["Your idea & plan", "Shot previews", "Video clips", "Final video"].map((label) => <Step key={label}><StepLabel>{label}</StepLabel></Step>)}
        </Stepper></Box>
        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, mb: 2, '& .MuiChip-root': { maxWidth: '100%' } }} aria-label="Your ad settings">
          <Chip label={AD_TYPES.find(type => type.id === (final?.ad_type || result?.ad_type))?.label || 'Character Commercial'} />
          {(result?.format?.duration_target_sec || draft?.target_duration_sec) && <Chip label={`${result?.format?.duration_target_sec || draft.target_duration_sec}s target`} />}
          {final?.aspect_ratio && <Chip label={final.aspect_ratio} />}
          {(draft?.characters || result?.continuity?.characters?.map(c => c.name) || []).map(name => <Chip key={name} label={name} variant="outlined" />)}
        </Box>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>Your progress is saved. You can return to this job from Job History.</Typography>
        {approved ? (
          audioReady && !previews.busy && !canResumePreviews ? (
            <FinalVideo data={result?.final_video} shots={shots} busy={assembling || result?.final_video?.status === "running"} onAssemble={handleAssemble} />
          ) : (
            <Card className="director-progress-card" data-testid="preview-progress">
              <div><p className="eyebrow">Shot previews</p><h2>{canResumePreviews ? (result?.video_prompt_error ? "Video instructions paused" : "Preparation paused") : result?.video_prompts_pending && previews.ready === previews.total ? "Your previews are ready to review" : "Preparing your previews"}</h2>
                <p>{result?.video_prompt_error ? "Your accepted previews are saved. We paused while preparing the instructions for your video clips; no videos have been generated yet." : result?.video_prompts_pending ? "Images appear as they're ready. We're also preparing instructions for your video clips; no videos are being generated yet." : "We prepare speech and timing, then create and check your images. No video clips are being generated yet."}</p>
                <p role="status">{previews.ready} of {previews.total} previews ready</p>
                {previews.busy && <ActionProgress label={result?.video_prompts_pending && shots.some(s => s.still_frame_status === "generating") ? "Creating previews and preparing video instructions…" : result?.video_prompts_pending ? "Preparing video instructions…" : shots.some(s => s.still_frame_status === "generating") ? "Creating and checking your images…" : "Preparing speech and shot details…"} />}
                {canResumePreviews && <Alert severity="warning">{result?.video_prompt_error ? "Click Retry video instructions to continue with your saved plan and previews." : previews.failed > 0 ? "Your plan and completed speech are saved. Create the missing previews in one step." : "Your plan, completed speech and accepted previews are saved. Retry preparation to continue."}</Alert>}
              </div>
            </Card>
          )
        ) : (
          <Card className="director-progress-card">
            <div className="director-progress-icon">
              {errored ? <AlertTriangle size={19} /> : done ? <Check size={19} /> : <Loader2 size={19} className="animate-spin" />}
            </div>
            <div>
              <p className="eyebrow">{done ? "Ready for review" : "Your video"}</p>
              <h2>{errored ? "Planning paused" : done ? (result?.qa?.approved === false ? "Review the highlighted plan details" : "Your written plan is ready") : draft ? "Checking your storyboard" : "Directing your story"}</h2>
              <p>{done ? (previews.ready === previews.total ? "Your preview images are ready too. Review the plan, then continue to video clips." : "Review and edit the shots below, then click 'Approve plan & create previews' to see them as images. This prepares any speech too; it does not generate video clips.") : errored ? "Your work is saved. Retry planning to continue." : draft ? "Preparing your editable Director plan." : progressNote || "Preparing the creative direction…"}</p>
              {!done && !errored && <ActionProgress label={draft ? "Checking your plan…" : `${progressNote}…`} />}
              {errored && !approved && <Button variant="contained" disabled={retrying} onClick={handleTimeoutRetry}>Retry planning</Button>}
            </div>
          </Card>
        )}
      </section>

      <Card component="section" className="shot-panel storyboard-panel" aria-label="Your storyboard">
        <div className="shot-panel-header">
          <div>
            <p className="eyebrow">Your storyboard</p>
            <h2>{shots.length ? `${shots.length} ${shots.length === 1 ? "shot" : "shots"}` : draft ? `${draft.shots.length} draft shots` : "Taking shape"}</h2>
          </div>
        </div>

        <details className="px-4 py-3 text-sm shrink-0">
          <summary className="cursor-pointer underline" style={{ color: COLORS.marigold }}>Advanced details</summary>
          <details><summary>Processing log</summary><pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{trace.map(event => `${event.agent_key}: ${event.note || ""}`).join("\n")}</pre></details>
          <div className="mt-3 flex flex-col gap-4 max-h-[30vh] overflow-y-auto">
            {CAMERA_VOCABULARY.map(({ key, label, options }) => (
              <section key={key} aria-label={`${label} vocabulary`}>
                <h3 className="font-semibold mb-2">{label}</h3>
                <dl className="flex flex-col gap-2">
                  {options.map(([value, description]) => <div key={value}><dt className="font-semibold">{value}</dt><dd style={{ color: COLORS.muted }}>{description}</dd></div>)}
                </dl>
              </section>
            ))}
          </div>
        </details>

        {castingWarnings.map((warning) => (
          <div key={warning} role="alert" className="m-3 rounded-lg border-2 border-amber-500 bg-amber-100 p-3 text-sm font-semibold text-amber-950">
            {friendlyMessage(warning, "Please review the selected voice before continuing.", { warning: true })}
          </div>
        ))}

        {!done && !errored && !shots.length && !draft && (
          <div className="panel-waiting">
            <Loader2 size={22} className="animate-spin" />
            <p>{progressNote || "The first shots will appear here when the plan is complete."}</p>
          </div>
        )}

        {compilerTimeout ? <Alert severity="warning" sx={{ m: 2 }} action={<Button color="inherit" disabled={retrying} onClick={handleTimeoutRetry}>{retrying ? "Retrying…" : "Retry"}</Button>}>
          <AlertTitle>This shot is taking longer than expected.</AlertTitle>
          {canResumePreviews ? "Your written plan and completed speech are saved. Retry continues preview preparation without generating your speech again." : "Planning stopped before it could finish. Retry starts a new attempt with the same brief, script and settings. Your original job stays in history."}
        </Alert> : errored && <Alert severity="error" sx={{ m: 2 }}>{result?.video_prompt_error ? "Your accepted previews are saved. Video instructions could not be prepared. Click Retry video instructions to continue." : friendlyMessage(final.error_message, "We couldn't finish your video plan. Please try again.")}</Alert>}
        {retrying && <ActionProgress label="Restarting your video plan…" />}
        {retryError && <Alert severity="error" sx={{ m: 2 }}>{friendlyMessage(retryError)}</Alert>}

        {draft && !shots.length && <StoryboardDraft draft={draft} stopped={errored} />}
        {result && shots.length > 0 && (
          <>
            {previews.failed > 0 && <Alert severity="warning" sx={{ m: 2 }}>{previews.failed} preview{previews.failed === 1 ? " is" : "s are"} missing. Your plan and completed speech are saved; use Create missing previews once. Existing previews are kept.</Alert>}
            <div className="panel-logline">
              <span>The story in one sentence</span>
              <p>{result.script.logline}</p>
            </div>

            <AdDirectionPlan direction={result.ad_direction} />
            <div className="qa-banner" data-approved={result.qa.approved}>
              <Check size={14} />
              <span>{result.qa.review_mode === "user" ? (result.qa.approved ? (approved ? "Plan approved by you" : "Technical checks passed · Review your story") : "Technical details need correction") : (result.qa.approved ? "Consistency checked" : "Some details need your review")} · {Math.round(result.assembly.total_duration_sec * 10) / 10}s{result.assembly.provisional ? " · Timing will update after audio" : ""}</span>
            </div>

            {result.qa.review_mode === "user" && <Box sx={{ px: 2, mb: 2 }}>
              <Typography variant="body2">Check that every story moment and complete spoken line is included. You approve the creative direction; technical checks do not judge the story.</Typography>
              {result.qa.issues?.map((issue, index) => <Alert severity="warning" key={index} sx={{ mt: 1 }}>Shot {issue.shot_number}: {issue.problem}</Alert>)}
            </Box>}
            <div className="shot-card-list" style={!approved ? { gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 340px), 1fr))" } : undefined}>
              {result.continuity?.characters?.some((character) => character.style_variant_id || character.style_variant_warning) && (
                <section className="shot-card shrink-0" aria-label="Character references for this style">
                  <p className="text-sm">Character references for this style</p>
                  {result.continuity.characters.filter((character) => character.style_variant_id || character.style_variant_warning).map((character) => (
                    <div key={character.name} className="mt-3 text-xs">
                      <p>{character.name}{character.visual_style ? ` · ${character.visual_style}` : ""}</p>
                      {character.style_variant_id && <img src={character.image_url} alt={`${character.name} — ${character.visual_style} reference`} className="mt-2 w-24 rounded-md" />}
                      {character.style_variant_source === "style_transfer_from_upload" && <p className="audio-warning">Style-transferred from an uploaded photo. Fidelity and quality can vary and are less reliable than a from-scratch generation.</p>}
                      {character.style_variant_warning && <p className="audio-warning">{friendlyMessage(character.style_variant_warning, "This character’s style needs your review.")}</p>}
                    </div>
                  ))}
                </section>
              )}
              {shots.map((shot) => {
                const isEditing = editingShot === shot.shot_number;
                const [visualStatus, outputLabel] = shotOutputStatus(shot, shotStatuses[shot.shot_number] || shot.status);
                return (
                  <Card component="article" className="shot-card" sx={isEditing ? { gridColumn: "1 / -1" } : undefined} key={shot.shot_number} data-shot-number={shot.shot_number}>
                    <div className="shot-card-title">
                      <span>{Math.round(shots.slice(0, shots.indexOf(shot)).reduce((sum, item) => sum + Math.max(0, Number(item.duration_sec) || 0), 0) * 10) / 10}s · Scene {shot.scene_number} · Shot {shot.shot_number} · {shot.has_dialogue ? "dialogue" : "silent"}</span>
                      <span className={`shot-status shot-status--${visualStatus}`}>{!approved ? (result.qa.issues?.some(issue => issue.shot_number === shot.shot_number) ? "Needs correction" : "Ready to review") : outputLabel}</span>
                    </div>

                    {shot.still_frame_url ? (
                      <figure className="my-3" aria-label={`Opening still for shot ${shot.shot_number}`}>
                        <ShotPreviewImage shot={shot} onOpen={() => setExpandedPreview(shot)} />
                        <figcaption className="mt-1 text-xs" style={{ color: COLORS.muted }}>Opening preview · click to enlarge</figcaption>
                      </figure>
                    ) : null}

                    {isEditing ? (
                      <><Alert severity="info" sx={{ mb: 2 }} data-testid="shot-plan-edit-impact">{approved || result.plan_edited_shots?.length ? "Saving reopens plan approval. This shot’s voice, preview and clip must be prepared again; other shots are kept. Nothing is generated until you approve." : "Save your changes, then approve the plan before creating previews."}</Alert><DirectorPlanEditor cast={(result.continuity?.characters || []).map(character => character.name)} speaking={shot.has_dialogue && shot.speech_mode !== "voiceover"} value={editValues} onChange={setEditValues} shotNumber={shot.shot_number} /></>
                    ) : (
                      <>
                        <p className="shot-description">{shot.description}</p>
                        {shot.dialogue_text && <p className="shot-dialogue">“{shot.dialogue_text}”</p>}
                      </>
                    )}

                    {!isEditing && <ShotDirectionPlan shot={shot} expanded={!approved} />}
                    {visualStatus === "error" && shot.error_message && <Alert severity="error">{friendlyMessage(shot.error_message, "This shot could not be completed. Please try again.")}</Alert>}
                    <div className="shot-characters">
                      <span>{shot.direction_version === 1 ? "Characters in this clip" : "Characters present"}</span>
                      <p>{shot.characters_in_shot?.length ? shot.characters_in_shot.join(", ") : "None"}</p>
                    </div>
                    {approved && (shot.still_frame_url || shot.preview_input || shot.compiled_prompt) && <ShotImageActions jobId={jobId} shot={shot} aspectRatio={result.aspect_ratio || final.aspect_ratio} disabled={previews.busy || editBlocksApproval || result.final_video?.status === "running" || ["submitting", "processing", "submission_unknown"].includes(shot.video_status)} onRefresh={async () => setFinal(await getJob(jobId))} />}
                    {shot.still_frame_status === "generating" ? <ActionProgress label={shot.still_frame_candidate ? `Verifying the saved preview for shot ${shot.shot_number}…` : `Creating and checking the preview for shot ${shot.shot_number}…`} /> : previewState(shot).key === "failed" && <Alert severity="warning" sx={{ my: 2 }}>
                      <AlertTitle>{shot.still_frame_error_kind === "verification" ? "Your image is saved — verification is unavailable" : shot.still_frame_error_kind === "mismatch" ? "We couldn't make a reliable preview automatically" : "This preview couldn't be created"}</AlertTitle>
                      {shot.still_frame_error_kind === "verification" ? "Retry verification to check the saved image without generating another one." : shot.still_frame_error_kind === "mismatch" ? (shot.still_retry_count ? "Automatic correction has already been tried. Upload an image or edit this shot to continue." : "We tried alternate instructions automatically. Try one corrected preview; you don't need to change anything.") : shot.video_url ? "Your existing video is still available below." : "Try this preview again. Video generation is a separate step."}
                      {approved && shot.compiled_prompt && !shot.video_url && !(shot.still_frame_error_kind === "mismatch" && shot.still_retry_count) && <Button data-testid={`retry-preview-${shot.shot_number}`} disabled={previews.busy || previewSubmitting !== null} onClick={() => handlePreviewRetry(shot)}>{shot.still_frame_error_kind === "verification" ? "Retry verification" : shot.still_frame_error_kind === "mismatch" ? "Try corrected preview" : "Retry preview"}</Button>}
                    </Alert>}

                    {shot.video_url && <ShotVideo shot={shot} />}
                    {shot.video_url && shot.video_source_changed && <Alert severity="info" sx={{ my: 2 }}>This video uses an earlier version of the shot. Click “Regenerate video” to use your current image before combining the final video.</Alert>}
                    <FaceEnhancement jobId={jobId} shot={shot} onRefresh={async () => setFinal(await getJob(jobId))} />
                    {shot.video_status && <Stack direction="row" spacing={1} alignItems="center" sx={{ my: 1 }}>
                      <p className="text-sm" role="status" style={{ margin: 0 }}>Video: {lockedAudioReview(shot) ? "Finishing saved video" : shot.video_source_changed && shot.video_status === "done" ? "Previous version — needs regeneration" : {done: "Ready", error: "Needs attention", failed: "Generation failed", review_required: "Needs review", processing: "Generating", submitting: "Starting", submission_unknown: "Checking the request"}[shot.video_status] || "Checking progress"}{shot.has_dialogue && shot.video_status === "done" ? ((shot.video_provider === "hedra" || shot.video_audio_model) ? " · Spoken performance with audio" : " · Voice recording has not been added yet") : ""}</p>
                      {!videoReady(shot) && ["processing", "submitting", "submission_unknown"].includes(shot.video_status) && <Button size="small" variant="outlined" startIcon={<RefreshCw size={14} />} disabled={statusRefreshing === shot.shot_number} onClick={() => refreshVideoStatus(shot)} data-testid={`refresh-video-status-${shot.shot_number}`}>
                        {statusRefreshing === shot.shot_number ? "Checking…" : "Check status"}
                      </Button>}
                    </Stack>}
                    {(videoSubmitting === shot.shot_number || regeneratingShot === shot.shot_number || ["submitting", "processing"].includes(shot.video_status)) && <ActionProgress label={shot.video_phase === "preparing_voice" ? `Preparing the saved voice for shot ${shot.shot_number}…` : `Generating video for shot ${shot.shot_number}…`} />}
                    {savingShot === shot.shot_number && <ActionProgress label="Saving your changes and checking the shot…" />}
                    {shot.video_source_changed && <p className="audio-warning">This video belongs to an earlier version of the shot plan.</p>}
                    {shot.video_status === "review_required" ? (() => {
                      const guidance = videoReviewGuidance(shot);
                      return <Alert severity="warning" data-testid={`video-review-required-${shot.shot_number}`}>
                        <AlertTitle>{guidance.title}</AlertTitle>
                        <p style={{ margin: 0 }}>{guidance.message}</p>
                        {guidance.detail && <p style={{ margin: '6px 0 0', fontWeight: 600 }}>{guidance.detail}</p>}
                        <p style={{ margin: '6px 0 0' }}>{guidance.action}</p>
                      </Alert>;
                    })() : shot.video_error && <Alert severity="error">{friendlyMessage(shot.video_error, "This video could not be completed. Please try again.")}</Alert>}
                    {videoReviewWarnings(shot).map((warning) => <Alert severity={["mismatch", "unverified"].includes(shot.video_speech_check?.status) ? "warning" : "info"} key={warning} data-testid={`video-review-note-${shot.shot_number}`}>{warning}</Alert>)}
                    {approved && !errored && (shot.has_dialogue || result.video_model || result.ai_model === "Seedance 2.0") && !shot.video_status && shot.compiled_prompt && shot.still_frame_url && !result.audio_assembly_pending && !result.assembly?.provisional && (!shot.has_dialogue || shot.dialogue_audio_url) && (
                      <Button id={`generate-video-${shot.shot_number}`} type="button" variant="contained" fullWidth startIcon={<Clapperboard size={18} />} sx={{ my: 2, minHeight: 48 }} disabled={editBlocksApproval || shot.still_frame_status === "generating" || videoSubmitting !== null || (!shot.has_dialogue && shot.duration_sec > 15)} onClick={() => handleVideo(shot.shot_number)}>
                        {videoSubmitting === shot.shot_number ? "Starting video…" : shot.has_dialogue ? "Generate speaking video" : `Generate video · ${Math.max(5, Math.ceil(shot.duration_sec))}s · ${result.quality || "720p"}`}
                      </Button>
                    )}
                    <div className="shot-technical">
                      <span><Clapperboard size={12} /> {shot.camera_angle} · {shot.camera_movement}</span>
                      <span><Clock3 size={12} /> {Number(shot.duration_sec.toFixed(1))}s · {shot.lighting}</span>
                    </div>

                    {(shot.video_url || ["error", "failed"].includes(shot.video_status) || (shot.video_status === "review_required" && !lockedAudioReview(shot))) && (
                      <div className="my-3 text-xs">
                        <label className="block">What would you like to change? (optional)
                          <textarea aria-label={`Regeneration change for shot ${shot.shot_number}`} maxLength={1000} value={videoHints[shot.shot_number] || ""} onChange={(event) => setVideoHints((current) => ({ ...current, [shot.shot_number]: event.target.value }))} className="block w-full rounded-md p-2 mt-1" style={{ background: COLORS.field, color: COLORS.text }} placeholder="For example: keep both people inside the cabin and make the lighting warmer" />
                        </label>
                        <p className="mt-2" style={{ color: COLORS.muted }}>Regenerate video will apply this request to this shot while preserving its approved image, characters, product and speech.</p>
                      </div>
                    )}

                    <div className="shot-card-actions">
                      {isEditing ? (
                        <>
                          <Button type="button" onClick={() => saveEdit(shot.shot_number)} disabled={savingShot === shot.shot_number} className="card-action card-action--primary">
                            {savingShot === shot.shot_number ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />} Save plan changes
                          </Button>
                          <Button type="button" onClick={() => setEditingShot(null)} className="card-action">Cancel</Button>
                        </>
                      ) : (
                        <>
                          <Button type="button" disabled={previews.busy || videoBusy || editBlocksApproval || shots.some(s => s.video_status === "submission_unknown" || s.preview_replacement?.status === "working")} onClick={() => beginEdit(shot)} className="card-action" aria-label={`Edit shot plan ${shot.shot_number}`}><Pencil size={13} /> Edit shot plan</Button>
                          {(shot.video_url || ["error", "failed"].includes(shot.video_status) || (shot.video_status === "review_required" && !lockedAudioReview(shot))) && <Button type="button" onClick={() => handleRegenerate(shot.shot_number)} disabled={editBlocksApproval || shot.still_frame_status === "generating" || !approved || regeneratingShot !== null || videoSubmitting !== null || result.audio_assembly_pending || result.assembly?.provisional || ["submitting", "processing", "submission_unknown"].includes(shot.video_status) || !shot.compiled_prompt || !shot.still_frame_url} className="card-action card-action--primary" style={{ background: COLORS.marigold, color: COLORS.bg }} aria-label={`Regenerate shot ${shot.shot_number}`} title="Restart this shot only">
                            {regeneratingShot === shot.shot_number ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />} Regenerate video
                          </Button>}
                        </>
                      )}
                    </div>
                  </Card>
                );
              })}
            </div>

            {approved && !previews.busy && audioReady && previews.ready > 0 && !errored && <p className="approved-note"><Check size={14} /> {allVideosReady ? "All clips ready to combine" : "Previews are images. Generate each video clip when you're ready."}</p>}
          </>
        )}

        {error && <Alert severity="error" sx={{ m: 2 }}>{friendlyMessage(error)}</Alert>}
      </Card>
      <PreviewLightbox shot={expandedPreview} shots={shots} onSelect={setExpandedPreview} onClose={() => setExpandedPreview(null)} />
      {(!done && !approved) && <Box component="footer" className="storyboard-wait-footer" role="status">{errored ? 'Planning paused · your work is saved' : draft ? 'Checking your draft · no images or videos are being generated yet' : 'Directing your story · your progress is saved'}</Box>}
      {(done || canResumePreviews) && shots.length > 0 && <Box component="footer" data-testid="job-primary-action" sx={{ position: "fixed", bottom: 0, left: { xs: 0, md: "var(--studio-rail-width, 236px)" }, right: 0, zIndex: 1100, bgcolor: "background.paper", borderTop: 1, borderColor: "divider", p: 2, display: "flex", flexWrap: "wrap", gap: 2, alignItems: "center", justifyContent: "space-between", boxShadow: "0 -6px 28px #0000000a" }}>
        <Box role="status">{editingShot !== null ? <>
          <Typography fontWeight={600}>Editing shot {editingShot}</Typography>
          <Typography id="shot-edit-approval-hint" variant="body2">{savingShot !== null ? "Saving and checking your edit…" : hasUnsavedEdit ? "Save your edit first" : "No unsaved changes"}</Typography>
        </> : canResumePreviews ? (result?.video_prompt_error ? "Video instructions paused · your previews are saved" : "Preview preparation paused · your plan is saved") : !approved ? (result?.qa?.approved === false ? "Correct the highlighted plan details before approval" : previews.ready ? `${previews.ready} of ${shots.length} previews ready` : "No previews yet · your written plan is ready") : previews.busy ? `${previews.ready} of ${shots.length} previews ready · working…` : `${shots.filter(videoReady).length} of ${shots.length} videos ready`}</Box>
        {canResumePreviews ? <Button variant="contained" disabled={retrying} onClick={handleTimeoutRetry}>{result?.video_prompt_error ? "Retry video instructions" : previews.failed > 0 ? "Create missing previews" : "Retry preview preparation"}</Button>
          : !approved ? <Button variant="contained" data-testid="create-previews" aria-describedby={editingShot !== null ? "shot-edit-approval-hint" : undefined} disabled={approving || editBlocksApproval || !result.qa.approved} onClick={handleApprove}>{approving ? "Starting…" : result.plan_edited_shots?.length ? "Approve edits & prepare changed shots" : previews.ready === shots.length ? "Generate video clips" : "Approve plan & create previews"}</Button>
          : previews.busy ? <span>Keep this page open or come back later.</span>
          : allVideosReady ? <Button variant="contained" disabled={editBlocksApproval || assembling || result.final_video?.status === "running"} onClick={handleAssemble}>{result.final_video?.url ? "Update final video" : "Create final video"}</Button>
          : previews.failed ? <span>Retry failed previews on their shot cards.</span>
          : previews.ready > 0 && audioReady && !errored ? <Stack spacing={0.5}>
              <Button variant="contained" data-testid="generate-clips-step" onClick={() => {
                const button = shots.map(shot => document.getElementById(`generate-video-${shot.shot_number}`)).find(Boolean);
                button?.scrollIntoView({ behavior: "smooth", block: "center" });
                button?.focus({ preventScroll: true });
              }}>Generate video clips</Button>
              <Typography variant="caption">Choose a shot to generate. Each clip starts only when you click its button.</Typography>
            </Stack>
          : <span>Preparing your shot previews…</span>}
      </Box>}
      <Box sx={{ height: { xs: 150, md: 100 }, gridColumn: "1 / -1" }} />
    </>
  );
}
