import { ArrowLeft, Clapperboard, Clock, FileText, Palette, Sparkles } from "lucide-react";
import { useState } from "react";

const COLORS = {
  bg: "#13141F",
  panel: "#1B1D2B",
  border: "#2E3145",
  text: "#F3F0E8",
  muted: "#9694A8",
  marigold: "#E8A33D",
};

const CONTENT_TYPES = ["Ad", "Short story", "Documentary", "Other"];
const DURATIONS = ["15 seconds", "30 seconds", "60 seconds"];
const VISUAL_STYLES = ["Natural", "Cinematic", "Cartoon / anime", "Realistic", "Hyper-realistic"];

function mentionsDuration(text) {
  return /\b\d+\s*(sec|secs|second|seconds|min|mins|minute|minutes)\b/i.test(text);
}

function ChoiceButton({ label, onClick }) {
  return (
    <button
      onClick={onClick}
      className="w-full text-left px-4 py-3 rounded-md text-sm font-medium transition-colors"
      style={{ background: COLORS.panel, border: `1px solid ${COLORS.border}`, color: COLORS.text }}
      onMouseEnter={(e) => (e.currentTarget.style.borderColor = COLORS.marigold)}
      onMouseLeave={(e) => (e.currentTarget.style.borderColor = COLORS.border)}
    >
      {label}
    </button>
  );
}

function StepShell({ icon: Icon, question, onBack, children }) {
  return (
    <div className="w-full max-w-xl">
      {onBack && (
        <button
          onClick={onBack}
          className="flex items-center gap-1 text-xs mb-6"
          style={{ color: COLORS.muted }}
        >
          <ArrowLeft size={14} />
          Back
        </button>
      )}
      <div className="flex items-center gap-3 mb-6">
        <Icon size={20} style={{ color: COLORS.marigold }} />
        <h2 className="text-xl font-medium" style={{ color: COLORS.text }}>
          {question}
        </h2>
      </div>
      {children}
    </div>
  );
}

export default function NewJob({ onSubmit }) {
  const [history, setHistory] = useState(["scriptReady"]);
  const stage = history[history.length - 1];
  const [answers, setAnswers] = useState({
    hasScript: null,
    text: "",
    contentType: null,
    duration: null,
  });
  const [customDuration, setCustomDuration] = useState("");
  const [showCustomDuration, setShowCustomDuration] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  function goTo(next) {
    setHistory((h) => [...h, next]);
  }
  function goBack() {
    setHistory((h) => (h.length > 1 ? h.slice(0, -1) : h));
  }

  function chooseScriptReady(hasScript) {
    setAnswers((a) => ({ ...a, hasScript }));
    goTo("input");
  }

  function submitText() {
    if (!answers.text.trim()) return;
    goTo("contentType");
  }

  function chooseContentType(contentType) {
    setAnswers((a) => ({ ...a, contentType }));
    goTo(mentionsDuration(answers.text) ? "visualStyle" : "duration");
  }

  function chooseDuration(duration) {
    setAnswers((a) => ({ ...a, duration }));
    goTo("visualStyle");
  }

  async function chooseVisualStyle(visualStyle) {
    setSubmitting(true);
    const parts = [answers.text.trim()];
    const meta = [];
    if (answers.contentType) meta.push(`Content type: ${answers.contentType}.`);
    if (answers.duration) meta.push(`Target duration: ${answers.duration}.`);
    meta.push(`Visual style: ${visualStyle}.`);
    if (meta.length) parts.push(meta.join(" "));
    try {
      await onSubmit(parts.join("\n\n"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="w-full min-h-screen flex flex-col items-center justify-center px-4">
      {stage === "scriptReady" && (
        <StepShell icon={Sparkles} question="Do you already have a script ready?">
          <div className="flex flex-col gap-3">
            <ChoiceButton label="Yes, I'll paste it" onClick={() => chooseScriptReady(true)} />
            <ChoiceButton label="No, write one for me from an idea" onClick={() => chooseScriptReady(false)} />
          </div>
        </StepShell>
      )}

      {stage === "input" && (
        <StepShell
          icon={FileText}
          question={answers.hasScript ? "Paste your script" : "Describe your idea"}
          onBack={goBack}
        >
          <textarea
            value={answers.text}
            onChange={(e) => setAnswers((a) => ({ ...a, text: e.target.value }))}
            placeholder={
              answers.hasScript
                ? "Paste the full script or scene breakdown here"
                : "A mythology-inspired ad for a jewellery brand, warm and dramatic"
            }
            rows={answers.hasScript ? 8 : 4}
            className="w-full rounded-md p-4 text-base outline-none resize-none mb-4"
            style={{ background: "#0F1019", border: `1px solid ${COLORS.border}`, color: COLORS.text }}
          />
          <button
            onClick={submitText}
            disabled={!answers.text.trim()}
            className="px-5 py-2.5 rounded-md text-sm font-medium"
            style={{
              background: answers.text.trim() ? COLORS.marigold : COLORS.border,
              color: answers.text.trim() ? COLORS.bg : COLORS.muted,
              cursor: answers.text.trim() ? "pointer" : "not-allowed",
            }}
          >
            Continue
          </button>
        </StepShell>
      )}

      {stage === "contentType" && (
        <StepShell icon={Clapperboard} question="What are you making?" onBack={goBack}>
          <div className="flex flex-col gap-3">
            {CONTENT_TYPES.map((t) => (
              <ChoiceButton key={t} label={t} onClick={() => chooseContentType(t)} />
            ))}
          </div>
        </StepShell>
      )}

      {stage === "duration" && (
        <StepShell icon={Clock} question="How long should the final video be?" onBack={goBack}>
          <div className="flex flex-col gap-3">
            {DURATIONS.map((d) => (
              <ChoiceButton key={d} label={d} onClick={() => chooseDuration(d)} />
            ))}
            {!showCustomDuration ? (
              <ChoiceButton label="Custom length" onClick={() => setShowCustomDuration(true)} />
            ) : (
              <div className="flex gap-2">
                <input
                  value={customDuration}
                  onChange={(e) => setCustomDuration(e.target.value)}
                  placeholder="e.g. 45 seconds"
                  className="flex-1 rounded-md p-3 text-sm outline-none"
                  style={{ background: "#0F1019", border: `1px solid ${COLORS.border}`, color: COLORS.text }}
                />
                <button
                  onClick={() => customDuration.trim() && chooseDuration(customDuration.trim())}
                  className="px-4 rounded-md text-sm font-medium"
                  style={{ background: COLORS.marigold, color: COLORS.bg }}
                >
                  OK
                </button>
              </div>
            )}
          </div>
        </StepShell>
      )}

      {stage === "visualStyle" && (
        <StepShell icon={Palette} question="What visual style?" onBack={goBack}>
          <div className="flex flex-col gap-3">
            {VISUAL_STYLES.map((s) => (
              <ChoiceButton
                key={s}
                label={submitting ? "Starting..." : s}
                onClick={() => !submitting && chooseVisualStyle(s)}
              />
            ))}
          </div>
        </StepShell>
      )}
    </div>
  );
}
