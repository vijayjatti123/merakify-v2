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

export function friendlyMessage(value, context = "This step could not finish. Please try again.", { warning = false } = {}) {
  if (!value) return "";
  const text = String(value);
  // Provider failures must never become speculative review advice because a
  // generic provider message happens to mention duration/framing/style.
  if (/invalid_parameters|invalid parameters/i.test(text)) return "The video service rejected this shot's inputs. No video was created. Retry video generation; your preview and approved speech are saved.";
  if (/speech reference|speech file/i.test(text) && /couldn't|cannot|does not match|exceeds|incomplete|empty/i.test(text)) return "The voice recording could not be prepared for video generation. No video request was sent. Your preview is saved; please retry.";
  if (!warning && /duration|pace|timing|calibrat/i.test(text)) return context;
  if (/compiler.*(deadline|timeout|timed out)/i.test(text)) return "This shot is taking longer than expected. Please retry planning.";
  if (/missing.*video|video.*missing|Generate.*shot\(s\)/i.test(text)) {
    const shots = text.match(/shot(?:\(s\)|s)?\s*[:#]?\s*([\d, ]+)/i)?.[1]?.trim();
    return shots ? `Finish generating video for shot(s) ${shots} before combining your video.` : "Finish generating each shot's video before combining your video.";
  }
  if (/gender|age_bracket|demographic|casting.classification/i.test(text)) return "The speaker’s age or gender was unclear. Voice selection used timing alone; please review the voice.";
  if (/aspect.ratio|dimensions|framing|composition/i.test(text)) return "The framing may differ from your request. Review this preview before continuing.";
  if (/style.*(mismatch|transfer|variant|fail)|photorealis|cel.shad/i.test(text)) return "The requested visual style may not match throughout. Please review the character and shot previews.";
  if (/dialogue.loss|protected|user.scripted/i.test(text)) return "Your script needs a closer review. Some consistency checks could not be resolved automatically.";
  if (warning && /duration|pace|timing|calibrat/i.test(text)) return "Speech timing may differ from the plan. Please listen to the result before approving.";
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

export function videoReviewWarnings(shot) {
  // Technical request details and routine notices remain in the saved trace.
  // A generic speech caution is shown once, beside the playable result only.
  if (!shot.video_url || shot.video_status !== 'done') return [];
  const result = [];
  const speechStatus = shot.video_speech_check?.status;
  if (speechStatus === 'mismatch') result.push("The generated speech does not match your approved dialogue. Automatic correction did not resolve it; review the clip before using it.");
  if (speechStatus === 'unverified') result.push("Your video is saved, but we couldn't verify its spoken words. Please listen before using it.");
  if (!['mismatch', 'unverified'].includes(speechStatus) && (shot.experimental_audio_sync || (shot.video_warnings || []).some(w => /^Audio-guided generation is experimental\./.test(w)))) result.push('Listen to the dialogue before approving: AI-generated speech may vary in pronunciation or mouth timing.');
  for (const warning of shot.video_warnings || []) {
    if (speechStatus && /Render compliance:.*speech/i.test(warning)) continue;
    if (/^Audio-guided generation is experimental\.|^Video duration is \d|^Provider bills \d/.test(warning)) continue;
    result.push(friendlyMessage(warning, 'Please review this video before approving it.', { warning: true }));
  }
  return [...new Set(result)];
}

export function videoReviewGuidance(shot) {
  const error = String(shot?.video_error || '');
  const hasVisualFinding = /staging|position|placement|inside|outside|style|appearance|identity|scale|framing|composition/i.test(error);
  if (/speech|dialogue|spoken/i.test(error) && !hasVisualFinding) return {
    title: 'Your approved dialogue is safe',
    message: 'The automatic listener could not confirm the words. The saved voice recording is applied directly, so you do not need to rewrite or control the speech.',
    action: 'The saved clip will be finished automatically without generating another video.',
  };
  if (/staging|position|placement|inside|outside/i.test(error)) {
    const stagingReason = error.match(/staging:\s*([^;]+)/i)?.[1]?.trim();
    const requiredDetail = stagingReason?.match(/required support\s+["“]([^"”]+)["”]/i)?.[1];
    return {
      title: 'The action or placement did not match',
      message: 'A person or object was not positioned as described in the approved shot.',
      detail: requiredDetail ? `Required detail not maintained: ${requiredDetail}.` : stagingReason,
      action: `${/speech|dialogue|spoken/i.test(error) ? 'Your approved voice recording remains saved. ' : ''}Describe the placement you want below, then regenerate this shot.`,
    };
  }
  if (/style|appearance|identity/i.test(error)) return {
    title: 'The visual result did not match',
    message: 'The clip did not preserve the approved character appearance or visual style closely enough.',
    action: 'Add the correction you want below, then regenerate this shot.',
  };
  if (/scale|framing|composition/i.test(error)) return {
    title: 'The framing did not match',
    message: 'The subject size or composition differed from the approved opening image.',
    action: 'Describe the framing you want below, then regenerate this shot.',
  };
  return {
    title: 'This clip needs another try',
    message: 'The generated clip did not match the approved shot closely enough.',
    action: 'Describe what should change below, then regenerate this shot.',
  };
}

export function progressMessage(event) {
  const key = event?.agent_key || "";
  if (key === "pipeline_timing" || key === "pipeline_usage") {
    try {
      const timing = JSON.parse(event.note);
      const label = progressMessage({ agent_key: timing.stage });
      if (timing.phase === "checkpoint_reused") return `${label} · saved work restored`;
      if (timing.phase === "waiting") return `${label} · ${Math.round(timing.elapsed_sec)} seconds elapsed`;
      if (timing.phase === "request_started" && timing.attempt > 1) return `${label} · retrying once`;
      return label;
    } catch { return "Preparing your plan"; }
  }
  if (key === "preview_timing") {
    try {
      const timing = JSON.parse(event.note);
      return `${/visual_check/.test(timing.phase) ? "Checking" : "Creating"} preview ${timing.shot_number}`;
    } catch { return "Creating your shot previews"; }
  }
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
