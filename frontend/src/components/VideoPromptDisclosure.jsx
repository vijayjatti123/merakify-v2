import { useState } from "react";
import { getShotVideoRequest } from "../api/client";

const withoutLinks = (value) => String(value || "").replace(/https?:\/\/\S+/g, "[reference image]");

export default function VideoPromptDisclosure({ jobId, shot }) {
  const [opened, setOpened] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [error, setError] = useState("");
  const saved = shot.video_retry_request?.prompt;
  async function onToggle(event) {
    if (!event.currentTarget.open) return;
    setOpened(true);
    if (saved || prompt) return;
    try {
      const translated = await getShotVideoRequest(jobId, shot.shot_number);
      setPrompt(translated.request?.prompt || "");
    } catch (problem) {
      setError(problem.message);
    }
  }
  return <details className="my-3 text-sm" onToggle={onToggle}>
    <summary className="cursor-pointer">Instructions sent to the video model</summary>
    {opened && <div className="mt-2 rounded-md border p-3" style={{ borderColor: "var(--mui-palette-divider)" }}>
      <p className="mb-2">These are the actual model instructions. Reference links are hidden here. Use “Edit this shot” to change the approved action and regenerate its instructions.</p>
      {error ? <p role="alert">{error}</p> : <pre className="whitespace-pre-wrap break-words font-sans">{withoutLinks(saved || prompt) || "Preparing instructions…"}</pre>}
    </div>}
  </details>;
}
