export const VIDEO_MODELS = [
  { id: "seedance_evolink", label: "Seedance 2.0 Standard — EvoLink", family: "Seedance 2.0" },
  { id: "seedance_fast_evolink", label: "Seedance 2.0 Fast — EvoLink", family: "Seedance 2.0" },
  { id: "seedance_mini_evolink", label: "Seedance 2.0 Mini — EvoLink", family: "Seedance 2.0" },
  { id: "seedance_fal", label: "Seedance 2.0 Standard — fal", family: "Seedance 2.0" },
  { id: "seedance_fast_fal", label: "Seedance 2.0 Fast — fal", family: "Seedance 2.0" },
  { id: "seedance_mini_fal", label: "Seedance 2.0 Mini — fal", family: "Seedance 2.0" },
  { id: "kling_voice_fal", label: "Kling 3 Voice ID — fal · 4K", family: "Kling 3.0" },
  { id: "kling_avatar_fal", label: "Kling Avatar — fal · Experimental", family: "Kling 3.0" },
];
export function modelNote(id) {
  if (id === "kling_voice_fal") return "All shots use Kling. English/Chinese speech only; always 4K. Generates new speech in the saved voice. First voice setup needs 5–30 seconds of approved speech and an extra setup call.";
  if (id === "kling_avatar_fal") return "Speaking shots only. Uses the preview and approved audio; scene motion, duration and resolution are model-controlled. Silent or narration-only shots cannot use this model.";
  return "All shots use this model. Speaking shots include the approved audio reference; review the generated words and timing. Fast and Mini support up to 720p.";
}
