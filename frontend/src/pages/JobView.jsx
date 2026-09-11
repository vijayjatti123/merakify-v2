import { AlertTriangle, Check, Clapperboard, Clock3, ImageIcon, Loader2, Pencil, RefreshCw, Save, Sparkles, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { approveJob, regenerateShot, reviseJob, streamJob } from "../api/client";
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

function StitchingPlaceholder() {
  return (
    <section className="generation-stage generation-placeholder" aria-live="polite">
      <Loader2 size={30} className="animate-spin" />
      <p className="eyebrow">All shots generated</p>
      <h2>Stitching...</h2>
      <p>Assembling the finished shots into one sequence.</p>
    </section>
  );
}

function FinalVideoPlaceholder() {
  return (
    <section className="generation-stage generation-placeholder generation-placeholder--final" aria-label="Final video placeholder">
      <Clapperboard size={32} />
      <p className="eyebrow">Sequence complete</p>
      <h2>Final video will appear here</h2>
      <p>Video rendering is not connected in Phase 1.</p>
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
  const [shotHints, setShotHints] = useState({});
  const [shotStatuses, setShotStatuses] = useState({});
  const [generationStage, setGenerationStage] = useState("shots");
  const stitchingTimerRef = useRef(null);
  const [streamCycle, setStreamCycle] = useState(0);

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

  useEffect(() => () => {
    clearTimeout(stitchingTimerRef.current);
  }, []);

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

  useEffect(() => {
    if (!approved || !shots.length || generationStage !== "shots") return;
    const everyShotDone = shots.every(
      (shot) => (shotStatuses[shot.shot_number] || shot.status || "pending") === "done",
    );
    if (!everyShotDone || result?.audio_assembly_pending || result?.assembly?.provisional) return;

    setGenerationStage("stitching");
    clearTimeout(stitchingTimerRef.current);
    stitchingTimerRef.current = setTimeout(() => {
      setGenerationStage("final");
      stitchingTimerRef.current = null;
    }, 1600);
  }, [approved, generationStage, shotStatuses, shots, result]);

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
      setGenerationStage("shots");
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
      setGenerationStage("shots");
      setFinal({ status: job.status, error_message: job.error_message, result: job.result });
      setStreamCycle((current) => current + 1);
    } catch (approveError) {
      setError(approveError.message);
    } finally {
      setApproving(false);
    }
  }

  async function handleRegenerate(shotNumber) {
    setRegeneratingShot(shotNumber);
    setError("");
    try {
      const job = await regenerateShot(jobId, shotNumber, shotHints[shotNumber]);
      clearTimeout(stitchingTimerRef.current);
      stitchingTimerRef.current = null;
      setGenerationStage("shots");
      setTrace([]);
      setShotStatuses(Object.fromEntries(job.result.shots.map((shot) => [shot.shot_number, shot.status])));
      setFinal({ status: job.status, error_message: job.error_message, result: job.result });
      setStreamCycle((current) => current + 1);
    } catch (regenerateError) {
      setError(regenerateError.message);
    } finally {
      setRegeneratingShot(null);
    }
  }

  return (
    <>
      <section className="generation-area">
        {approved ? (
          generationStage === "stitching" ? (
            <StitchingPlaceholder />
          ) : generationStage === "final" ? (
            <FinalVideoPlaceholder />
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
                    <div className="reference-image-slot" aria-label={`Reference image for shot ${shot.shot_number}`}>
                      <ImageIcon size={18} />
                      <span>Reference image</span>
                      <small>Not attached</small>
                    </div>
                    <div className="shot-technical">
                      <span><Clapperboard size={12} /> {shot.camera_angle} · {shot.camera_movement}</span>
                      <span><Clock3 size={12} /> {shot.duration_sec}s · {shot.lighting}</span>
                    </div>

                    <details className="my-3 text-xs">
                      <summary className="cursor-pointer" style={{ color: COLORS.marigold }}>Regenerate hints (optional)</summary>
                      <p className="my-2" style={{ color: COLORS.muted }}>The Director may adapt or decline a hint to preserve continuity.</p>
                      <div className="grid grid-cols-2 gap-2">
                        {CAMERA_VOCABULARY.map(({ key, label, options }) => (
                          <label key={key} className="flex flex-col gap-1">{label}
                            <select aria-label={`${label} hint for shot ${shot.shot_number}`} value={shotHints[shot.shot_number]?.[key] || ""} disabled={regeneratingShot === shot.shot_number} onChange={(event) => setShotHints((current) => ({ ...current, [shot.shot_number]: { ...current[shot.shot_number], [key]: event.target.value } }))} className="rounded-md p-2 min-w-0" style={{ background: COLORS.field, color: COLORS.text, border: `1px solid ${COLORS.border}` }}>
                              <option value="">No hint</option>
                              {options.map(([value, description]) => <option key={value} value={value} title={description}>{value}</option>)}
                            </select>
                          </label>
                        ))}
                      </div>
                    </details>

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
                          <button type="button" onClick={() => beginEdit(shot)} className="card-action" aria-label={`Edit shot ${shot.shot_number}`}><Pencil size={13} /> Edit</button>
                          <button type="button" onClick={() => handleRegenerate(shot.shot_number)} disabled={!approved || regeneratingShot === shot.shot_number} className="card-action" aria-label={`Regenerate shot ${shot.shot_number}`} title={approved ? "Restart this shot only" : "Approve the plan first"}>
                            {regeneratingShot === shot.shot_number ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />} Regenerate
                          </button>
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
