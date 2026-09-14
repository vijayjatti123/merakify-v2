import { AlertTriangle, Check, Clapperboard, Clock3, ImageIcon, Loader2, Pencil, RefreshCw, Save, Sparkles, X } from "lucide-react";
import { useEffect, useState } from "react";

import { approveJob, reviseJob, streamJob, getJob, generateShotVideo, regenerateShotVideo, assembleFinalVideo } from "../api/client";
import FaceEnhancement, { ShotVideo } from "../components/FaceEnhancement";
import { CAMERA_VOCABULARY } from "../utils/cameraVocabulary";

const COLORS = {
  bg: "#13141F",
  panel: "#1B1D2B",
  field: "#0F1019",
  border: "#2E3145",
  text: "#F3F0E8",
  muted: "#9694A8",
  marigold: "#E8A33D",
  green: "#7FA37A",
  red: "#C1453B",
};

const STATUS_LABELS = {
  pending: "Pending",
  generating: "Generating",
  done: "Done",
  error: "Error",
};

function GenerationGrid({ shots, statuses }) {
  return (
    <section className="generation-stage" aria-label="Shot generation progress">
      <div className="generation-heading">
        <div>
          <p className="eyebrow">Approved shot plan</p>
          <h2>Generating your sequence</h2>
        </div>
        <span className="phase-badge">Voice generation</span>
      </div>
      <div className="generation-grid">
        {shots.map((shot) => {
          const status = statuses[shot.shot_number] || shot.status || "pending";
          return (
            <article
              key={shot.shot_number}
              className={`generation-card generation-card--${status}`}
              data-shot-number={shot.shot_number}
              data-status={status}
            >
              <div className="generation-frame">
                {status === "error" ? <AlertTriangle size={18} /> : <Sparkles size={18} />}
                <span>Shot {shot.shot_number}</span>
              </div>
              <div className="generation-meta">
                <span>{STATUS_LABELS[status] || status}</span>
                <span>{shot.duration_sec}s</span>
              </div>
              <div className="generation-progress"><span /></div>
            </article>
          );
        })}
      </div>
    </section>
  );
}

function FinalVideo({ data, shots, busy, onAssemble }) {
  const missing = shots.filter((shot) => !shot.video_url || shot.video_status !== "done" || shot.video_source_changed).map((shot) => shot.shot_number);
  return (
    <section className="generation-stage generation-placeholder generation-placeholder--final" aria-label="Final video assembly" aria-live="polite">
      {busy ? <Loader2 size={30} className="animate-spin" /> : <Clapperboard size={32} />}
      <p className="eyebrow">Final timeline</p>
      <h2>{busy ? "Assembling final video…" : data?.url ? "Your assembled video" : "Assemble your finished shots"}</h2>
      {data?.url && <video controls preload="metadata" src={data.url} className="w-full rounded-md my-3" style={{ maxHeight: "60vh" }} aria-label="Final assembled video" />}
      {data?.url && <a href={data.url} target="_blank" rel="noreferrer" className="underline">Open final video</a>}
      {data?.stale && <p className="audio-warning">Shot videos or transitions have changed. Assemble again to update the final video.</p>}
      {data?.error && <p className="panel-error" role="alert">{data.error}</p>}
      {missing.length > 0 && <p>Generate or regenerate video for shot(s) {missing.join(", ")} before final assembly.</p>}
      <button type="button" className="approve-button mt-4 disabled:opacity-50 disabled:cursor-not-allowed" disabled={busy || !shots.length || missing.length > 0} onClick={onAssemble}>
        {busy ? "Assembling…" : data?.url ? "Reassemble final video" : "Assemble final video"}
      </button>
      <p>Uses the existing shot audio and planned cuts or crossfades.</p>
    </section>
  );
}

export default function JobView({ jobId, onReset }) {
  const [trace, setTrace] = useState([]);
  const [final, setFinal] = useState(null);
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
  const [videoHints, setVideoHints] = useState({});
  const videoBusy = final?.result?.shots?.some((shot) => (["submitting", "processing"].includes(shot.video_status) || ["queued", "running"].includes(shot.face_enhancement?.status))) || final?.result?.final_video?.status === "running";

  useEffect(() => {
    if (!videoBusy) return;
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
  }, [jobId, videoBusy]);

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
  const approved = Boolean(result?.generation_approved);
  const latestTrace = trace[trace.length - 1];
  const castingWarnings = [...new Set([
    ...trace.filter((event) => event.agent_key === "casting_warning").map((event) => event.note),
    ...(result?.continuity?.characters || []).map((character) => character.casting_warning).filter(Boolean),
  ])];

  const audioReady = shots.length > 0 && shots.every((shot) => (shotStatuses[shot.shot_number] || shot.status) === "done") && !result?.audio_assembly_pending && !result?.assembly?.provisional;

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
    setEditValues({ description: shot.description || "", dialogue_text: shot.dialogue_text || "" });
    setError("");
  }

  async function saveEdit(shotNumber) {
    setSavingShot(shotNumber);
    setError("");
    try {
      const job = await reviseJob(jobId, [{ shot_number: shotNumber, ...editValues }]);
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

  async function handleRegenerate(shotNumber, editVideo = false) {
    const hint = editVideo ? (videoHints[shotNumber] || "").trim() : "";
    if (editVideo && !hint) return;
    setRegeneratingShot(shotNumber);
    setError("");
    try {
      const shot = shots.find((item) => item.shot_number === shotNumber);
      await regenerateShotVideo(jobId, shot, hint);
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
      <section className="generation-area">
        {approved ? (
          audioReady ? (
            <FinalVideo data={result?.final_video} shots={shots} busy={assembling || result?.final_video?.status === "running"} onAssemble={handleAssemble} />
          ) : (
            <GenerationGrid shots={shots} statuses={shotStatuses} />
          )
        ) : (
          <div className="director-progress-card">
            <div className="director-progress-icon">
              {done ? <Check size={19} /> : <Loader2 size={19} className="animate-spin" />}
            </div>
            <div>
              <p className="eyebrow">{done ? "Ready for review" : "Director pipeline"}</p>
              <h2>{done ? "Your shot plan is ready" : "Building your shot plan"}</h2>
              <p>{latestTrace?.note || "Preparing the creative direction…"}</p>
            </div>
          </div>
        )}
      </section>

      <aside className="shot-panel" aria-label="Shot plan">
        <div className="shot-panel-header">
          <div>
            <p className="eyebrow">Shot plan</p>
            <h2>{done ? `${shots.length} shots` : "In progress"}</h2>
          </div>
          <button type="button" onClick={onReset} className="icon-button" aria-label="Start over"><X size={17} /></button>
        </div>

        <details className="px-4 py-3 text-sm shrink-0">
          <summary className="cursor-pointer underline" style={{ color: COLORS.marigold }}>Camera guide</summary>
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
            {warning}
          </div>
        ))}

        {!done && !errored && (
          <div className="panel-waiting">
            <Loader2 size={22} className="animate-spin" />
            <p>{latestTrace?.note || "The first shots will appear here when the plan is complete."}</p>
          </div>
        )}

        {errored && <p className="panel-error">{final.error_message || "Something went wrong."}</p>}

        {done && result && (
          <>
            <div className="panel-logline">
              <span>Logline</span>
              <p>{result.script.logline}</p>
            </div>

            <div className="qa-banner" data-approved={result.qa.approved}>
              <Check size={14} />
              <span>{result.qa.approved ? "Continuity approved" : "Residual QA notes"} · {result.assembly.total_duration_sec}s{result.assembly.provisional ? " · Provisional timing — finalized after audio" : ""}</span>
            </div>

            <div className="shot-card-list">
              {result.continuity?.characters?.some((character) => character.style_variant_id || character.style_variant_warning) && (
                <section className="shot-card shrink-0" aria-label="Character references for this style">
                  <p className="text-sm">Character references for this style</p>
                  {result.continuity.characters.filter((character) => character.style_variant_id || character.style_variant_warning).map((character) => (
                    <div key={character.name} className="mt-3 text-xs">
                      <p>{character.name}{character.visual_style ? ` · ${character.visual_style}` : ""}</p>
                      {character.style_variant_id && <img src={character.image_url} alt={`${character.name} — ${character.visual_style} reference`} className="mt-2 w-24 rounded-md" />}
                      {character.style_variant_source === "style_transfer_from_upload" && <p className="audio-warning">Style-transferred from an uploaded photo. Fidelity and quality can vary and are less reliable than a from-scratch generation.</p>}
                      {character.style_variant_warning && <p className="audio-warning">{character.style_variant_warning}</p>}
                    </div>
                  ))}
                </section>
              )}
              {shots.map((shot) => {
                const isEditing = editingShot === shot.shot_number;
                const visualStatus = shotStatuses[shot.shot_number] || shot.status || "pending";
                return (
                  <article className="shot-card" key={shot.shot_number} data-shot-number={shot.shot_number}>
                    <div className="shot-card-title">
                      <span>Shot {shot.shot_number} · {shot.has_dialogue ? "dialogue" : "silent"}</span>
                      <span className={`shot-status shot-status--${visualStatus}`}>{STATUS_LABELS[visualStatus] || visualStatus}</span>
                    </div>

                    {isEditing ? (
                      <div className="shot-edit-fields">
                        <label>Description<textarea value={editValues.description} onChange={(event) => setEditValues((current) => ({ ...current, description: event.target.value }))} /></label>
                        <label>Dialogue<textarea value={editValues.dialogue_text} onChange={(event) => setEditValues((current) => ({ ...current, dialogue_text: event.target.value }))} /></label>
                      </div>
                    ) : (
                      <>
                        <p className="shot-description">{shot.description}</p>
                        {shot.dialogue_text && <p className="shot-dialogue">“{shot.dialogue_text}”</p>}
                      </>
                    )}

                    {shot.experimental_audio_sync && <p className="audio-warning">Experimental audio sync</p>}
                    {visualStatus === "error" && shot.error_message && <p className="panel-error">{shot.error_message}</p>}
                    <div className="shot-characters">
                      <span>Characters present</span>
                      <p>{shot.characters_in_shot?.length ? shot.characters_in_shot.join(", ") : "None"}</p>
                    </div>
                    {shot.still_frame_url ? (
                      <figure className="my-3" aria-label={`Opening still for shot ${shot.shot_number}`}>
                        <img src={shot.still_frame_url} alt={`Shot ${shot.shot_number} opening frame — ${shot.description}`} className="w-full rounded-md" loading="lazy" />
                        <figcaption className="mt-1 text-xs" style={{ color: COLORS.muted }}>Opening still preview</figcaption>
                      </figure>
                    ) : (
                      <div className="reference-image-slot" aria-label={`Still preview for shot ${shot.shot_number}`}>
                        <ImageIcon size={18} /><span>Opening still</span>
                        <small>{shot.still_frame_warning ? "Unavailable" : "Available after final shot planning"}</small>
                      </div>
                    )}
                    {shot.still_frame_warning && <p className="audio-warning">{shot.still_frame_warning}</p>}
                    {shot.video_url && <ShotVideo shot={shot} />}
                    <FaceEnhancement jobId={jobId} shot={shot} onRefresh={async () => setFinal(await getJob(jobId))} />
                    {shot.video_status && <p className="text-xs my-2" role="status">Video: {shot.video_status.replaceAll("_", " ")}{shot.has_dialogue && shot.video_status === "done" ? (shot.video_provider === "hedra" ? " · Hedra dialogue · audio included" : " · Silent clip; Sarvam audio awaits later muxing") : ""}</p>}
                    {shot.video_source_changed && <p className="audio-warning">This video belongs to an earlier version of the shot plan.</p>}
                    {shot.video_error && <p className="panel-error">{shot.video_error}</p>}
                    {(shot.video_warnings || []).map((warning) => <p className="audio-warning" key={warning}>{warning}</p>)}
                    {(shot.has_dialogue || result.ai_model === "Seedance 2.0") && !shot.video_status && shot.compiled_prompt && (shot.still_frame_url || (shot.has_dialogue && result.continuity?.characters?.some((character) => character.character_id && character.image_url && shot.characters_in_shot?.includes(character.name)))) && !result.audio_assembly_pending && !result.assembly?.provisional && (!shot.has_dialogue || shot.dialogue_audio_url) && (
                      <button type="button" className="card-action my-2" disabled={videoSubmitting !== null || (!shot.has_dialogue && shot.duration_sec > 15)} onClick={() => handleVideo(shot.shot_number)}>
                        {videoSubmitting === shot.shot_number ? "Submitting video…" : shot.has_dialogue ? "Generate dialogue video · Hedra · 720p" : `Generate video · ${Math.max(4, Math.ceil(shot.duration_sec))}s · ${result.quality || "720p"}`}
                      </button>
                    )}
                    <div className="shot-technical">
                      <span><Clapperboard size={12} /> {shot.camera_angle} · {shot.camera_movement}</span>
                      <span><Clock3 size={12} /> {shot.duration_sec}s · {shot.lighting}</span>
                    </div>

                    {shot.compiled_prompt?.trim() && (
                      <details className="my-3 text-xs">
                        <summary className="cursor-pointer" style={{ color: COLORS.muted }}>View technical prompt</summary>
                        <p className="mt-2 whitespace-pre-wrap break-words leading-relaxed" style={{ overflowWrap: "anywhere" }}>{shot.compiled_prompt}</p>
                      </details>
                    )}

                    {!shot.has_dialogue && shot.video_provider !== "hedra" && shot.video_url && (
                      <div className="my-3 text-xs">
                        <label className="block">What should change? (required for Edit Shot)
                          <textarea required aria-label={`Edit hint for shot ${shot.shot_number}`} maxLength={700} value={videoHints[shot.shot_number] || ""} onChange={(event) => setVideoHints((current) => ({ ...current, [shot.shot_number]: event.target.value }))} className="block w-full rounded-md p-2 mt-1" style={{ background: COLORS.field, color: COLORS.text }} placeholder="For example: make the lighting warmer" />
                        </label>
                        <p className="mt-2" style={{ color: COLORS.muted }}>May affect framing and composition beyond the requested change.</p>
                      </div>
                    )}

                    <div className="shot-card-actions">
                      {isEditing ? (
                        <>
                          <button type="button" onClick={() => saveEdit(shot.shot_number)} disabled={savingShot === shot.shot_number} className="card-action card-action--primary">
                            {savingShot === shot.shot_number ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />} Save & recheck
                          </button>
                          <button type="button" onClick={() => setEditingShot(null)} className="card-action">Cancel</button>
                        </>
                      ) : (
                        <>
                          <button type="button" onClick={() => beginEdit(shot)} className="card-action" aria-label={`Edit shot text ${shot.shot_number}`}><Pencil size={13} /> Edit text</button>
                          <button type="button" onClick={() => handleRegenerate(shot.shot_number)} disabled={!approved || regeneratingShot !== null || videoSubmitting !== null || result.audio_assembly_pending || result.assembly?.provisional || ["submitting", "processing", "submission_unknown"].includes(shot.video_status) || !shot.compiled_prompt} className="card-action card-action--primary" style={{ background: COLORS.marigold, color: COLORS.bg }} aria-label={`Regenerate shot ${shot.shot_number}`} title={approved ? "Restart this shot only" : "Approve the plan first"}>
                            {regeneratingShot === shot.shot_number ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />} Regenerate
                          </button>
                          {!shot.has_dialogue && shot.video_provider !== "hedra" && shot.video_url && (
                            <button type="button" onClick={() => handleRegenerate(shot.shot_number, true)} disabled={!(videoHints[shot.shot_number] || "").trim() || !approved || regeneratingShot !== null || videoSubmitting !== null || result.audio_assembly_pending || result.assembly?.provisional || ["submitting", "processing", "submission_unknown"].includes(shot.video_status) || !shot.compiled_prompt} className="card-action" style={{ background: "transparent", border: `1px solid ${COLORS.border}`, fontSize: "0.62rem", padding: "0.3rem 0.45rem" }} aria-label={`Edit shot video ${shot.shot_number}`}>
                              Edit Shot
                            </button>
                          )}
                        </>
                      )}
                    </div>
                  </article>
                );
              })}
            </div>

            {!approved && (
              <div className="approve-footer">
                <p>Approve the plan to generate real dialogue audio.</p>
                <button type="button" onClick={handleApprove} disabled={approving} className="approve-button">
                  {approving ? <Loader2 size={15} className="animate-spin" /> : <Check size={15} />}
                  {approving ? "Approving…" : "Approve shot plan"}
                </button>
              </div>
            )}

            {approved && <p className="approved-note"><Check size={14} /> Plan approved · voice generation active</p>}
          </>
        )}

        {error && <p className="panel-error">{error}</p>}
      </aside>
    </>
  );
}
