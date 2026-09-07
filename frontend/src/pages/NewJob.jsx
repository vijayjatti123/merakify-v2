import { Film } from "lucide-react";
import { useState } from "react";

export default function NewJob({ onSubmit }) {
  const [brief, setBrief] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit() {
    if (!brief.trim()) return;
    setSubmitting(true);
    try {
      await onSubmit(brief.trim());
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="w-full min-h-screen flex flex-col items-center justify-center px-4">
      <div className="w-full max-w-xl">
        <h1
          className="text-4xl mb-3"
          style={{ fontFamily: "'Fraunces', serif", fontWeight: 500, letterSpacing: "-0.01em" }}
        >
          Merakify
        </h1>
        <p className="text-base mb-8" style={{ color: "#9694A8" }}>
          Say what you want made. One input, one agent, no forms.
        </p>
        <textarea
          value={brief}
          onChange={(e) => setBrief(e.target.value)}
          placeholder="A 30-second mythology-inspired ad for a jewellery brand, warm and dramatic"
          rows={4}
          className="w-full rounded-md p-4 text-base outline-none resize-none"
          style={{ background: "#0F1019", border: "1px solid #2E3145", color: "#F3F0E8" }}
        />
        <button
          onClick={handleSubmit}
          disabled={!brief.trim() || submitting}
          className="flex items-center gap-2 mt-5 px-5 py-2.5 rounded-md text-sm font-medium"
          style={{
            background: brief.trim() && !submitting ? "#E8A33D" : "#2E3145",
            color: brief.trim() && !submitting ? "#13141F" : "#9694A8",
            cursor: brief.trim() && !submitting ? "pointer" : "not-allowed",
          }}
        >
          <Film size={16} />
          {submitting ? "Starting..." : "Make it"}
        </button>
      </div>
    </div>
  );
}
