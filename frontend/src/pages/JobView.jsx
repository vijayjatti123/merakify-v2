import { AlertTriangle, Check, Clapperboard, Clock3, ImageIcon, Loader2, Pencil, RefreshCw, Save, Sparkles, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { approveJob, regenerateShot, reviseJob, streamJob } from "../api/client";

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
        <span className="phase-badge">Phase 1 preview</span>
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
  const [shotStatuses, setShotStatuses] = useState({});
  const [generationStage, setGenerationStage] = useState("shots");
  const timersRef = useRef(new Map());
  const stitchingTimerRef = useRef(null);
  const generationStartedRef = useRef(false);

  useEffect(() => {
    const source = streamJob(jobId, {
      onEvent: (event) => setTrace((current) => [...current, event]),
      onFinal: (payload) => setFinal(payload),
      onError: () => setError("The live progress connection was interrupted."),
    });
    return () => source.close();
  }, [jobId]);

  useEffect(() => () => {
    timersRef.current.forEach((timers) => timers.forEach(clearTimeout));
    timersRef.current.clear();
    clearTimeout(stitchingTimerRef.current);
  }, []);

  const done = final?.status === "done";
  const errored = final?.status === "error";
  const result = final?.result;
  const shots = result?.shots || [];
  const approved = Boolean(result?.generation_approved);
  const latestTrace = trace[trace.length - 1];

  function clearShotTimers(shotNumber) {
    const timers = timersRef.current.get(shotNumber) || [];
    timers.forEach(clearTimeout);
    timersRef.current.delete(shotNumber);
  }

  function queueFakeProgress(shotNumber, delay = 0) {
    clearShotTimers(shotNumber);
    setShotStatuses((current) => ({ ...current, [shotNumber]: "pending" }));
    const startTimer = setTimeout(() => {
      setShotStatuses((current) => ({ ...current, [shotNumber]: "generating" }));
    }, delay + 80);
    const finishTimer = setTimeout(() => {
      setShotStatuses((current) => ({ ...current, [shotNumber]: "done" }));
      timersRef.current.delete(shotNumber);
    }, delay + 1880);
    timersRef.current.set(shotNumber, [startTimer, finishTimer]);
  }

  useEffect(() => {
    if (!approved || !shots.length || generationStartedRef.current) return;
    generationStartedRef.current = true;
    shots
      .filter((shot) => !["done", "error"].includes(shot.status))
      .forEach((shot, index) => queueFakeProgress(shot.shot_number, index * 760));
  }, [approved, shots.length]);

  useEffect(() => {
    if (!approved || !shots.length || generationStage !== "shots") return;
    const everyShotDone = shots.every(
      (shot) => (shotStatuses[shot.shot_number] || shot.status || "pending") === "done",
    );
    if (!everyShotDone) return;

    setGenerationStage("stitching");
    clearTimeout(stitchingTimerRef.current);
    stitchingTimerRef.current = setTimeout(() => {
      setGenerationStage("final");
      stitchingTimerRef.current = null;
    }, 1600);
  }, [approved, generationStage, shotStatuses, shots]);

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
      timersRef.current.forEach((timers) => timers.forEach(clearTimeout));
      timersRef.current.clear();
      generationStartedRef.current = false;
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
      generationStartedRef.current = false;
      setShotStatuses({});
      setGenerationStage("shots");
      setFinal({ status: job.status, error_message: job.error_message, result: job.result });
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
      await regenerateShot(jobId, shotNumber);
      clearTimeout(stitchingTimerRef.current);
      stitchingTimerRef.current = null;
      setGenerationStage("shots");
      queueFakeProgress(shotNumber);
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
              <span>{result.qa.approved ? "Continuity approved" : "Residual QA notes"} · {result.assembly.total_duration_sec}s</span>
            </div>

            <div className="shot-card-list">
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
                <p>Approve the plan to start the Phase 1 generation preview.</p>
                <button type="button" onClick={handleApprove} disabled={approving} className="approve-button">
                  {approving ? <Loader2 size={15} className="animate-spin" /> : <Check size={15} />}
                  {approving ? "Approving…" : "Approve shot plan"}
                </button>
              </div>
            )}

            {approved && <p className="approved-note"><Check size={14} /> Plan approved · generation preview active</p>}
          </>
        )}

        {error && <p className="panel-error">{error}</p>}
      </aside>
    </>
  );
}
