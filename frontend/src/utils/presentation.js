// Presentation only: never rewrite stored prompts, user text or API payloads.
export const JARGON_REPLACEMENTS = [
  ["Shot Prompt Compiler", "Preparing shot details"], ["Format Classifier", "Understanding your idea"],
  ["Script Architect", "Writing your story"], ["Cinematography", "Planning camera and lighting"],
  ["Continuity Agent", "Keeping characters and places consistent"], ["Shot Assembler", "Putting it all together"],
  ["Assembly", "Putting it all together"], ["Director pipeline", "Planning your video"],
  ["Logline", "The story in one sentence"], ["Residual QA notes", "Some details need your review"],
  ["Continuity approved", "Consistency checked"], ["Provisional timing", "Timing will update after audio"],
  ["Resolve every named reference", "Choose your characters and places"], ["Extracting", "Reading your script"],
  ["Reference sheet", "Character views"], ["Reference asset", "Reference image"],
  ["Experimental audio sync", "Speech timing is experimental"], ["muxing", "adding the voice recording"],
  ["Hedra dialogue", "Spoken performance"], ["Sarvam voice", "Character voice"],
  ["casting classification", "speaker age and gender"], ["deflicker", "smoothing brightness"],
];

export function friendlyMessage(value, context = "This step could not finish. Please try again.") {
  if (!value) return "";
  const text = String(value);
  if (/compiler.*(deadline|timeout|timed out)/i.test(text)) return "This shot is taking longer than expected. Please retry planning.";
  if (/missing.*video|video.*missing|Generate.*shot\(s\)/i.test(text)) {
    const shots = text.match(/shot(?:\(s\)|s)?\s*[:#]?\s*([\d, ]+)/i)?.[1]?.trim();
    return shots ? `Finish generating video for shot(s) ${shots} before combining your video.` : "Finish generating each shot's video before combining your video.";
  }
  if (/gender|age_bracket|demographic|casting.classification/i.test(text)) return "The speaker’s age or gender was unclear. Voice selection used timing alone; please review the voice.";
  if (/aspect.ratio|dimensions|framing|composition/i.test(text)) return "The framing may differ from your request. Review this preview before continuing.";
  if (/style.*(mismatch|transfer|variant|fail)|photorealis|cel.shad/i.test(text)) return "The requested visual style may not match throughout. Please review the character and shot previews.";
  if (/dialogue.loss|protected|user.scripted/i.test(text)) return "Your script needs a closer review. Some consistency checks could not be resolved automatically.";
  if (/duration|pace|timing|calibrat/i.test(text)) return "Speech timing may differ from the plan. Please listen to the result before approving.";
  if (/reference.to.video|mode.intent|intent.classification/i.test(text)) return "The video may interpret your reference differently. Please review its framing and movement.";
  if (/S3|presign|expired|access.denied|403/i.test(text)) return "This media could not be loaded. Refresh to request a new link.";
  if (/429|rate.limit|capacity|overload/i.test(text)) return "The generation service is busy. Please try again shortly.";
  if (/insufficient|credits|balance|billing/i.test(text)) return "Generation is unavailable because the service needs credits. Please contact the workspace owner.";
  if (/enhanc|restor|Replicate|GFPGAN/i.test(text)) return "Face enhancement could not finish. Your original video remains available.";
  if (/unverif|not.verified|confidence/i.test(text)) return "Automatic checks could not confirm this result. Please review it before continuing.";
  if (/out.of.date|earlier.version|stale/i.test(text)) return "This preview belongs to an earlier version. Generate it again to reflect your changes.";
  // Only pass through known, plain UI messages. Provider exceptions/JSON/URLs
  // remain in the stored trace; they never become customer-facing copy.
  if (/^(Add a character|Save your voice|Could not |Failed to load|Choose |Please |May affect |The live progress connection)/.test(text) && !/[{}]|https?:|Traceback|Error:|Module [A-Z]/.test(text)) return text;
  return context;
}

export function progressMessage(event) {
  const key = event?.agent_key || "";
  if (/format/.test(key)) return "Understanding your idea";
  if (/shot_prompt_compiler/.test(key)) return "Preparing shot details";
  if (/still/.test(key)) return "Creating your shot previews";
  if (/voice|audio|shot_status/.test(key)) return "Preparing your voice recordings";
  if (/script/.test(key)) return "Writing your story";
  if (/continuity|character/.test(key)) return "Keeping characters and places consistent";
  if (/cinematography|fix/.test(key)) return "Planning camera and lighting";
  if (/qa/.test(key)) return "Checking the plan for consistency";
  if (/assembly/.test(key)) return "Putting it all together";
  return "Planning your video";
}
