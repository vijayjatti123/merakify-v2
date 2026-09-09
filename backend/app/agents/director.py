import json
from typing import Callable

from sqlalchemy.orm import Session

from app.agents import prompts
from app.agents.llm_client import call_agent
from app.services import job_service

EventFn = Callable[[str, str], None]


def _attach_voice_refs(shots: list[dict], characters: list[dict], narrator_voice_ref=None) -> list[dict]:
    """Attach each shot's character voice references deterministically, in code,
    rather than trusting the model to copy a reference string unchanged across
    several JSON round trips. The Cinematography Agent only has to name which
    characters are in a shot (characters_in_shot); this looks up the actual
    voice_sample_ref for each name from the continuity library.

    A shot can carry dialogue with no character in it at all — voiceover or
    narration, nobody visible on-screen speaking. That case surfaced for real
    the first time this pipeline ran against a real ad brief: has_dialogue
    true, characters_in_shot empty, voice_refs left as an empty dict with
    nowhere for a real voice-generation call to look. Every such shot gets
    the job's one narrator_voice_ref attached under the "Narrator" key here,
    the same deterministic-attachment principle as character voices — never
    left for a model to remember to fill in.

    Also flags every dialogue-carrying shot as experimental: by product
    decision, Indic dialogue audio (Sarvam/ElevenLabs) is passed straight into
    the video model as reference audio, with no dedicated lip-sync pass
    afterward. That's simpler and cheaper, but the video model's own lip-sync
    accuracy for Indic phonemes, and whether it preserves the reference audio
    exactly, are both unverified — see AUDIO_SYNC_DISCLAIMER. This flag is set
    here, in code, on every shot with has_dialogue true, not left to the model
    to remember to mention.
    """
    by_name = {c["name"]: c.get("voice_sample_ref") for c in characters}
    for shot in shots:
        names = shot.get("characters_in_shot", [])
        shot["voice_refs"] = {name: by_name.get(name) for name in names if name in by_name}
        if shot.get("has_dialogue") and not names:
            shot["voice_refs"]["Narrator"] = narrator_voice_ref
        shot["experimental_audio_sync"] = bool(shot.get("has_dialogue"))
        if shot["experimental_audio_sync"]:
            shot["audio_sync_note"] = AUDIO_SYNC_DISCLAIMER
    return shots


AUDIO_SYNC_DISCLAIMER = (
    "Experimental: Indic dialogue audio is sent to the video model as reference "
    "audio directly, with no dedicated lip-sync pass. Accuracy is not assured."
)


def run_pipeline(db: Session, job_id: str) -> None:
    """Runs the full pipeline for one job, synchronously, writing an AgentEvent
    row (and a job status update) after every step so a live SSE stream reading
    the same rows sees progress in near real time. Intended to be called from a
    background task, not from inside the request/response cycle.
    """

    def emit(agent_key: str, note: str) -> None:
        job_service.append_event(db, job_id, agent_key, note)

    try:
        job_service.set_status(db, job_id, "running")
        job = job_service.get_job(db, job_id)
        brief = job.brief

        # 1. Format Classifier — cheap/fast model, this step is pure classification.
        emit("format", "Reading the request, choosing format and structure...")
        fmt = call_agent(prompts.FORMAT_CLASSIFIER, brief, fast=True)
        emit("format", f"Classified as {fmt['format']}, {fmt['structure']} structure, {fmt['num_scenes']} scenes.")

        # 2. Script Architect
        emit("script", "Writing scene breakdown...")
        script = call_agent(
            prompts.SCRIPT_ARCHITECT,
            f"Brief: {brief}\nFormat: {fmt['format']}\nStructure: {fmt['structure']}\nNumber of scenes: {fmt['num_scenes']}",
        )
        emit("script", f"Logline locked: \"{script['logline']}\"")

        # 3. Visual Continuity Agent — builds the reference library BEFORE any
        # shot is planned, so every later step can be checked against it.
        emit("continuity_plan", "Building the reference asset library before any shot is planned...")
        continuity = call_agent(prompts.CONTINUITY_AGENT, json.dumps(script["scenes"]))
        emit(
            "continuity_plan",
            f"Locked {len(continuity['characters'])} character(s), {len(continuity['locations'])} location(s) as identity anchors.",
        )

        # 4. Cinematography Agent — the largest output in the pipeline (one
        # object per shot, several fields each), so it gets a bigger token
        # budget than the default rather than risking truncation. It also
        # needs the target runtime explicitly — without it, there's nothing
        # stopping the total from drifting well past what was asked for.
        emit("cinematography", "Assigning camera, lens and lighting per shot...")
        cine = call_agent(
            prompts.CINEMATOGRAPHY_AGENT,
            f"Scenes: {json.dumps(script['scenes'])}\nCharacters: {json.dumps(continuity['characters'])}"
            f"\nTarget total duration: {fmt['duration_target_sec']} seconds",
            max_tokens=4096,
        )
        cine["shots"] = _attach_voice_refs(cine["shots"], continuity["characters"], continuity.get("narrator_voice_ref"))
        dialogue_shots = sum(1 for s in cine["shots"] if s.get("has_dialogue"))
        cutaway_shots = len(cine["shots"]) - dialogue_shots
        emit(
            "cinematography",
            f"Drafted {len(cine['shots'])} shot(s): {dialogue_shots} carrying dialogue, "
            f"{cutaway_shots} silent cutaway/reaction shot(s).",
        )

        # 5. Continuity QA Agent, with one autonomous self-correction loop.
        # This is the part that actually makes the system agentic rather than
        # a single generative call: it inspects its own prior output and can
        # send work back for revision with no human in the loop.
        emit("qa", "Checking the shot list for continuity and film-grammar violations...")
        qa = call_agent(
            prompts.QA_AGENT,
            f"Shots: {json.dumps(cine['shots'])}\nCharacters: {json.dumps(continuity['characters'])}",
            max_tokens=3072,
        )

        if not qa.get("approved") and qa.get("issues"):
            emit("qa", f"Found {len(qa['issues'])} issue(s) — sending back to Cinematography, no human needed.")
            for issue in qa["issues"]:
                emit("qa", f"Shot {issue['shot_number']}: {issue['problem']}")

            emit("cinematography", "Revising flagged shots per QA feedback...")
            cine = call_agent(
                prompts.CINEMATOGRAPHY_FIX,
                f"Current shots: {json.dumps(cine['shots'])}\nRequired fixes: {json.dumps(qa['issues'])}",
                max_tokens=4096,
            )
            cine["shots"] = _attach_voice_refs(cine["shots"], continuity["characters"], continuity.get("narrator_voice_ref"))
            emit("cinematography", "Revision complete.")

            emit("qa", "Re-checking the revised shot list...")
            qa = call_agent(
                prompts.QA_AGENT,
                f"Shots: {json.dumps(cine['shots'])}\nCharacters: {json.dumps(continuity['characters'])}",
                max_tokens=3072,
            )
            emit("qa", "Approved — continuity holds." if qa.get("approved") else "Residual notes remain; proceeding with best version.")
        else:
            emit("qa", "Approved on first pass — no continuity issues found.")

        # 6. Shot Assembler
        emit("assembly", "Sequencing shots and choosing transitions...")
        assembly = call_agent(prompts.SHOT_ASSEMBLER, json.dumps(cine["shots"]))
        emit("assembly", f"Runtime locked at {assembly['total_duration_sec']}s.")

        # 7. Duration self-check — the same inspect-judge-act shape as the QA
        # loop above, applied to total runtime. Without this, nothing ever
        # verifies the assembled total against what was actually requested;
        # real runs have overshot a 30s target by 33% and a 15s target by
        # over 100% with no error raised, because summing durations correctly
        # isn't the same as checking the sum against a target. One retry,
        # same cap as the QA loop, for the same reason: bound the cost of an
        # autonomous correction rather than looping indefinitely.
        target = fmt["duration_target_sec"]
        actual = assembly["total_duration_sec"]
        if actual > target * 1.15:
            emit(
                "assembly",
                f"{actual}s overshoots the {target}s target — sending back to Cinematography to trim, no human needed.",
            )
            cine = call_agent(
                prompts.CINEMATOGRAPHY_TRIM,
                f"Current shots: {json.dumps(cine['shots'])}\nTarget total duration: {target} seconds\n"
                f"Current total: {actual} seconds",
                max_tokens=4096,
            )
            cine["shots"] = _attach_voice_refs(cine["shots"], continuity["characters"], continuity.get("narrator_voice_ref"))
            emit("cinematography", "Trimmed to fit the target runtime.")

            emit("assembly", "Re-sequencing the trimmed shot list...")
            assembly = call_agent(prompts.SHOT_ASSEMBLER, json.dumps(cine["shots"]))
            emit(
                "assembly",
                f"Runtime now {assembly['total_duration_sec']}s (target {target}s)."
                if assembly["total_duration_sec"] <= target * 1.15
                else f"Still {assembly['total_duration_sec']}s after one trim pass; proceeding with best version.",
            )
        else:
            emit("assembly", f"{actual}s is within range of the {target}s target — no trim needed.")

        # TODO (next milestone, not this skeleton): fan out here to real asset
        # generation — one call per character/location in continuity, run
        # concurrently with asyncio.gather rather than sequentially.
        #
        # TODO (rendering, depends on the above): for each shot, call the
        # chosen video model. For shots with has_dialogue true, pass the
        # character's voice_sample_ref audio (Sarvam/ElevenLabs) directly as
        # the model's reference-audio input (e.g. Seedance's reference_audios)
        # so the model drives both video and lip movement from that audio in
        # one call. By product decision, there is NO separate lip-sync pass
        # (Sync Labs/HeyGen) in the default pipeline — every shot with
        # experimental_audio_sync true (set in _attach_voice_refs) should
        # surface AUDIO_SYNC_DISCLAIMER wherever it's shown to the user,
        # since neither the video model's lip-sync accuracy for Indic
        # phonemes nor its preservation of the input audio's exact rhythm is
        # verified. Revisit this the moment real output is reviewable: if it
        # holds up, keep it; if it visibly drifts, that's the signal to add a
        # dedicated lip-sync step back in for dialogue shots specifically,
        # not to silently patch the disclaimer instead of the pipeline.

        result = {
            "format": fmt,
            "script": script,
            "continuity": continuity,
            "shots": cine["shots"],
            "assembly": assembly,
            "qa": qa,
        }
        job_service.set_result(db, job_id, result)
        job_service.set_status(db, job_id, "done")

    except Exception as exc:  # noqa: BLE001 — surface any failure to the job record
        job_service.set_status(db, job_id, "error", error_message=str(exc))
        emit("error", f"Pipeline failed: {exc}")
