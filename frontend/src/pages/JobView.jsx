import { Aperture, Check, Eye, Layers, ListVideo, Loader2, RotateCcw, ScrollText } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { streamJob } from "../api/client";

const AGENTS = [
  { key: "format", label: "Format", icon: Layers },
  { key: "script", label: "Script", icon: ScrollText },
  { key: "continuity_plan", label: "Continuity", icon: Eye },
  { key: "cinematography", label: "Cinematography", icon: Aperture },
  { key: "qa", label: "QA", icon: Check },
  { key: "assembly", label: "Assembly", icon: ListVideo },
];

export default function JobView({ jobId, onReset }) {
  const [trace, setTrace] = useState([]);
  const [activeAgent, setActiveAgent] = useState(null);
  const [final, setFinal] = useState(null);
  const bottomRef = useRef(null);

  useEffect(() => {
    const source = streamJob(jobId, {
      onEvent: (ev) => {
        setActiveAgent(ev.agent_key);
        setTrace((t) => [...t, ev]);
      },
      onFinal: (payload) => {
        setActiveAgent(null);
        setFinal(payload);
      },
      onError: () => {},
    });
    return () => source.close();
  }, [jobId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [trace]);

  const done = final?.status === "done";
  const errored = final?.status === "error";

  return (
    <div className="w-full min-h-screen flex flex-col items-center px-4 py-10">
      <div className="w-full max-w-3xl flex flex-col gap-6">
        <div className="flex flex-wrap gap-2">
          {AGENTS.map((a) => {
            const Icon = a.icon;
            const isActive = activeAgent === a.key;
            const touched = trace.some((t) => t.agent_key === a.key);
            return (
              <div
                key={a.key}
                className="flex items-center gap-2 px-3 py-1.5 rounded-full text-xs"
                style={{
                  background: isActive ? "#E8A33D" : touched ? "#1B1D2B" : "transparent",
                  border: `1px solid ${isActive ? "#E8A33D" : "#2E3145"}`,
                  color: isActive ? "#13141F" : touched ? "#F3F0E8" : "#9694A8",
                }}
              >
                {isActive ? <Loader2 size={12} className="animate-spin" /> : <Icon size={12} />}
                {a.label}
              </div>
            );
          })}
        </div>

        <div
          className="rounded-lg p-5 flex flex-col gap-2 max-h-64 overflow-y-auto"
          style={{ background: "#1B1D2B", border: "1px solid #2E3145" }}
        >
          {trace.map((t, i) => {
            const agent = AGENTS.find((a) => a.key === t.agent_key);
            return (
              <div key={i} className="text-sm flex gap-2">
                <span style={{ color: "#E8A33D", minWidth: "120px" }}>{agent ? agent.label : t.agent_key}</span>
                <span style={{ color: "#9694A8" }}>{t.note}</span>
              </div>
            );
          })}
          <div ref={bottomRef} />
        </div>

        {errored && (
          <p className="text-sm" style={{ color: "#C1453B" }}>
            {final.error_message || "Something went wrong."}
          </p>
        )}

        {done && final.result && (
          <div className="flex flex-col gap-6">
            <div>
              <h3 className="text-sm mb-2" style={{ color: "#9694A8" }}>
                Logline
              </h3>
              <p className="text-base" style={{ fontFamily: "'Fraunces', serif" }}>
                {final.result.script.logline}
              </p>
            </div>

            <div>
              <h3 className="text-sm mb-3" style={{ color: "#9694A8" }}>
                Shot list
              </h3>
              <div className="flex gap-3 overflow-x-auto pb-2">
                {final.result.shots.map((sh) => (
                  <div
                    key={sh.shot_number}
                    className="rounded-lg p-4 shrink-0"
                    style={{ width: "220px", background: "#1B1D2B", border: "1px solid #2E3145" }}
                  >
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-xs" style={{ color: "#9694A8" }}>
                        Shot {sh.shot_number}
                        {sh.has_dialogue ? " · dialogue" : " · silent"}
                      </span>
                      <span className="text-xs" style={{ color: "#E8A33D" }}>
                        {sh.duration_sec}s
                      </span>
                    </div>
                    <p className="text-sm mb-2">{sh.description}</p>
                    {sh.has_dialogue && sh.dialogue_text && (
                      <p className="text-xs mb-2 italic" style={{ color: "#E8A33D" }}>
                        "{sh.dialogue_text}"
                      </p>
                    )}
                    {sh.experimental_audio_sync && (
                      <div
                        className="text-xs mb-2 px-2 py-1 rounded"
                        style={{ background: "#2E2418", color: "#E8A33D", border: "1px solid #4A3A20" }}
                      >
                        ⚠ Experimental — audio sync accuracy not assured
                      </div>
                    )}
                    <div className="text-xs flex flex-col gap-1" style={{ color: "#9694A8" }}>
                      <span>
                        {sh.camera_angle} · {sh.camera_movement}
                      </span>
                      <span>{sh.lighting}</span>
                      {sh.characters_in_shot?.length > 0 && <span>{sh.characters_in_shot.join(", ")}</span>}
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <p className="text-sm flex items-center gap-2" style={{ color: final.result.qa.approved ? "#7FA37A" : "#C1453B" }}>
              <Check size={14} />
              {final.result.qa.approved ? "Continuity approved" : "Proceeded with residual notes"} · total runtime{" "}
              {final.result.assembly.total_duration_sec}s
            </p>
          </div>
        )}

        {(done || errored) && (
          <button
            onClick={onReset}
            className="flex items-center gap-2 px-5 py-2.5 rounded-md text-sm font-medium self-start"
            style={{ background: "transparent", border: "1px solid #2E3145", color: "#F3F0E8" }}
          >
            <RotateCcw size={15} />
            Start over
          </button>
        )}
      </div>
    </div>
  );
}
