# Merakify Core — v2 skeleton

This is a from-scratch rebuild of the core loop only: **brief in → Director
Agent pipeline runs autonomously → shot list out.** Nothing else from the old
platform is here on purpose. See the "What's deliberately not here" section.

## How it works

1. `POST /api/jobs` with `{"brief": "..."}` creates a job and immediately
   kicks off the pipeline as a background task. The request returns right
   away with a job id — no client ever blocks on the full pipeline run.
2. The frontend opens `GET /api/jobs/{id}/stream` (Server-Sent Events) and
   renders each agent's progress live, instead of a spinner.
3. Six agents run in sequence: Format Classifier → Script Architect →
   Visual Continuity → Cinematography → Continuity QA → Shot Assembler.
   QA can send work back to Cinematography once, autonomously, if it finds
   a continuity or film-grammar problem — no human approves that step.
4. The final shot list, scenes, and QA verdict are stored on the job row
   and streamed to the client as a `final` SSE event.

## Running it locally

**Backend**
```
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in ANTHROPIC_API_KEY
uvicorn app.main:app --reload --port 8000
```

**Frontend**
```
cd frontend
npm install
cp .env.example .env
npm run dev
```

Open the Vite dev server URL, type a brief, watch it run.

## Deploying

- Backend: Railway, using the included `Procfile`. Set `DATABASE_URL` to
  Railway's provided Postgres connection string — nothing else in the code
  changes, since `app/db.py` reads that env var directly.
- Frontend: Railway (or Vercel) as a static build (`npm run build`), with
  `VITE_BACKEND_URL` pointing at the deployed backend.
- Object storage uses the private S3 integration in
  `app/services/storage_service.py`. Reads use time-limited presigned S3 URLs,
  so the bucket remains private.

## What's deliberately not here

Everything from the old platform that isn't "turn a brief into a shot list":
screenwriting tools, producer/crew/budget/callsheet features, the music and
songwriting studio, the YouTube series tool, market research and pitch deck
generators. None of it belongs in this core surface. If any of it has real
users today, it can keep living on the old codebase as a separate, legacy
product — it shouldn't be ported into this one.

Also not here yet, on purpose, because they're the next milestone rather than
part of the skeleton:

- **Real asset generation.** `director.py` has a `TODO` marking exactly where
  to fan out to Ideogram/Nano Banana for character and location references
  (run concurrently with `asyncio.gather`, one call per character/location —
  don't do this sequentially), and where per-shot video generation plugs in
  before assembly.
- **Dialogue confined to one shot per scene.** The Cinematography Agent
  follows the approach already proven in the old codebase's Ad Studio Alpha:
  a scene's dialogue lives in exactly one shot (`has_dialogue`/
  `dialogue_text`); if a scene needs more screen time than one 9-second shot
  covers, the extra shots are silent visual beats — a reaction, a cutaway, a
  product detail — never a continuation of the same line. This sidesteps
  ever needing to stitch one voice across multiple generations. The QA Agent
  enforces it (flags any scene with dialogue in more than one shot, or any
  shot over 9 seconds) through the same self-correction loop as its other
  continuity checks.
- **Indic dialogue audio, sent straight to the video model — labeled experimental, by product decision.**
  No dedicated lip-sync step (Sync Labs/HeyGen) runs by default. Instead, a
  dialogue shot's cloned voice audio (Sarvam/ElevenLabs) is passed directly to
  the video-generation call as reference audio (e.g. Seedance's
  `reference_audios`), letting the model drive video and mouth movement from
  that audio in one call. This is simpler and cheaper than a three-step
  pipeline, at a known, accepted cost: neither the video model's lip-sync
  accuracy for Indic phonemes nor its exact preservation of the input audio's
  rhythm is verified. `director.py`'s `_attach_voice_refs` sets
  `experimental_audio_sync: true` and an `audio_sync_note` on every shot with
  `has_dialogue` — deterministically, in code, not left to a model to
  remember — and the frontend surfaces it as a visible warning on the shot
  card. If real output review shows this drifting badly, the fix is adding a
  dedicated lip-sync step back in for dialogue shots specifically — not
  quietly dropping the disclaimer instead of fixing the pipeline.
- **Object storage consumers.** Direct S3 upload/download/delete and private
  URL generation now live in `app/services/storage_service.py`; the asset
  generation milestone still needs to call that service when it starts
  producing character, location, and rendered-video files.
- **Auth.** There's no login on this skeleton at all. Add real Google OAuth
  (your own `client_id`/`client_secret`, not a managed proxy) before this
  goes anywhere near real users.
- **Multi-instance SSE.** The stream endpoint polls its own database every
  400ms, which is correct for a single backend instance. If you ever scale
  to multiple instances behind a load balancer, swap that polling loop for a
  Redis pub/sub subscription — the event shape sent to the frontend doesn't
  need to change.

## Phase 3 — mandatory still-image review

Module O advances the real asset generation milestone with per-shot opening
stills after the Shot Prompt Compiler. Silent jobs generate stills after
Assembly; dialogue jobs wait for audio approval and final Assembly. No video
generation is performed. Decoded aspect ratios must match the requested ratio
within 2% to allow provider resolution rounding. A mismatch emits a trace
warning and uses the existing one-retry budget; exhausted retries leave a null
still URL without blocking the job. Dimension checks do not verify camera
angles, subject cropping or composition.

Framing requests are not guaranteed to be honored. Automated visual QA can miss or accept discrepancies in shot scale, subject placement and cropping. A passed QA result does not establish exact framing compliance.

Before trusting still generation broadly, mandatory Phase 3 review must inspect
real stills across multiple categories against their compiled prompts and
reference images: framing, cropping, subject placement, identity, visual style
and opening-action state. Compiler text approval and still-image QA approval
are separate checks; neither replaces reviewing the actual images.

## Why it's structured this way

- **One route file, one orchestrator file.** The old codebase had four
  competing implementations of ad creation. This skeleton is built so there
  can only ever be one, on purpose — new capability should extend
  `director.py`'s pipeline, not create a parallel path next to it.
- **Model routing is already in the client.** `llm_client.py` routes
  classification to a fast/cheap model and reasoning-heavy steps to a
  stronger one. Keep that discipline as you add steps — don't default
  everything to the expensive model.
- **The QA self-correction loop is the load-bearing piece**, not a demo
  flourish. It's what makes this an agentic system rather than a single
  generative call with a UI around it. Any future pipeline change should
  preserve a real agent inspecting another agent's output and being able to
  act on what it finds, without a human in that specific loop.

## Module AQ: cached voice previews

Character creation offers cached previews for the existing 37 Sarvam voices in English,
Hindi, Tamil, Telugu, and Bengali (the five named language choices on the job form).
The free-text Other choice has no implied preview support. Preview language does not
change character identity, voice assignment, or a job's language. Preview playback
never synthesizes audio; it fetches private S3 WAV files using freshly signed URLs.

Run this explicit maintenance command from backend with the existing SARVAM_API_KEY
and AWS settings in .env (or environment):

    python -m app.services.generate_voice_previews --output ./preview-audit

It uses bulbul:v3, pace=1, 24 kHz WAV, with three concurrent requests and one bounded
retry on failure. It writes voice-previews/v1/<language>/<voice_id>.wav and per-language
manifests. Completed entries are skipped on rerun. Do not run concurrent batch processes.
No application startup or preview endpoint calls this command.

For one newly added catalog voice (after updating the existing backend/frontend catalogs):

    python -m app.services.generate_voice_previews --voices NEW_ID --output ./preview-audit

To extend languages, add a native-script PREVIEW_TEXT entry and the UI language choice,
then run with --languages LANGUAGE. Use a new VERSION for changes to existing sample
text/model/parameters so already-cached samples are not silently replaced.

GET /api/voice-previews?language=English only reads the S3 manifest and signs URLs.
URLs expire after 30 minutes; the picker refreshes them before playback after 25 minutes.
Missing previews disable only the play icon, not voice selection or Save voice.
Samples are reusable object-storage assets; no per-preview TTS charge applies.
This extends character review, not the generation pipeline or voice calibration.


### Module AR — narration without a Vault character

Shots retain `has_dialogue` for all spoken audio and add `speech_mode`:
`onscreen` uses Hedra, `voiceover` uses Seedance visuals plus approved Sarvam
narration at final assembly, and `none` is silent/ambient. Legacy spoken shots
with no visible characters are treated as voice-over. Visible bystanders can
coexist with an explicit `voiceover` mode; they are not assigned the narration.
The job's existing `continuity.narrator_voice_ref` is the stable catalog voice,
with no Character Vault row. The existing audio approval flow still applies.

Final assembly replaces the silent B-roll audio with that exact recording
before the existing cuts/crossfades, correction, grade, and deflicker steps.
Audio is not synthesized or retimed during assembly. Raw shot clips remain
silent until assembly. The measured narration must fit Seedance's existing
15-second limit; an overlong complete line or a returned clip shorter than its
audio raises a specific error rather than silently clipping speech. Longer
narration spanning multiple visual clips is not implemented by this module.

This extends README's core brief-to-finished-video workflow; it adds no new
provider, orchestrator, or Vault submission flow. Module K protection remains
unchanged and applies equally to supplied narration text.


### Native speaking-video models and job-level selection

The setup selector stores an optional `video_model` on each job, independently
of the Compiler's `ai_model` family. All shots inherit the saved provider/tier;
refresh, script intake, and job retries preserve it. Existing jobs retain their
legacy nullable selection. Seedance 2.0 Standard/Fast/Mini are available through
EvoLink and fal. Fast/Mini are limited to 480p/720p.

Visible speaking shots now use the accepted scene preview plus approved audio
with Seedance, or experimental Kling Avatar. There is no new external lip-sync
pass and no portrait fallback. Historical Hedra tasks remain recoverable.
Kling 3 Voice ID uses the accepted scene preview, approved dialogue text, and a
reusable provider voice ID. Its endpoint outputs 4K and supports English/Chinese;
other languages are rejected to avoid automatic translation. First registration
requires 5–30 seconds of approved single-speaker audio; later shots reuse the ID.
It generates a new performance, not the exact Sarvam recording/timing. Avatar
cannot render silent/narration-only shots; scene Kling and Seedance can.

Set `FAL_API_KEY` on the backend (`FAL_API-KEY` is accepted as a compatibility
alias). Durable voice/video claims prevent blind resubmission after uncertain
paid calls. No automatic model fallback is performed. Request/persistence/UI
regressions are tested; real rendered voice fidelity and sync remain unverified
for the new routes. This extends the existing core rendering workflow rather
than introducing another orchestrator.

### Approved product references

The intake screen accepts up to four named products separately from characters.
Upload a photo, select its product area, optionally prepare a clean background
through fal `pixelcut/product-photo`, then explicitly approve the prepared image
or cropped original. Preparation uses the existing server-side `FAL_API_KEY`;
the UI shows the published approximate per-image charge before submission.
Original, cropped and prepared images are stored separately in S3. Approved
products are immutable and reusable without further preparation charges.

New `products` and `job_products` tables use the existing metadata startup
creation pattern. Jobs snapshot approved product keys, including script intake
and retries. Preview generation and visual QA receive those locked references
within the existing reference cap. Existing packaging lettering is preserved;
invented captions remain prohibited. Video requests keep the accepted scene
image as their starting/reference image, never the standalone product cutout.

Preparation uses a durable single-submission claim and a saved fal request ID.
Opening a pending product resumes polling. An uncertain submission is not
automatically resubmitted; failures retain the original and offer an explicit
original-image choice. Review is necessary: cutouts may retain unwanted props,
and generative scenes cannot guarantee pixel-perfect small packaging text.
This extends the README real-asset-generation milestone and existing core
workflow; it introduces no character voice or second Director pipeline.

### Script-aware creative direction

AI refinement is available in both idea and script intake. It assesses the
product benefit, audience, intended takeaway, execution, script ambiguity, tone,
differentiator and exclusions, using the current eight toolbar settings and
selected approved product names. It asks one targeted question at a time, up to
five; unresolved or unverified details cannot receive a confident-ready status.
Provider failure retains the visible generic-question fallback. Text calls have
a 50-second request budget and one optional truncation recovery inside it.

Script refinement produces separate, editable production notes; it never replaces
the source script. On explicit acceptance, session ID/revision resolve to an
immutable job snapshot in the optional `creative_direction_json` column. The
existing startup migration adds that column to existing SQLite/Postgres jobs.
The single Director supplies reviewed direction and answers to planning; the
Compiler receives the same direction, and job retries retain it. No media is
generated merely by refining or accepting direction. This extends core intake
and the existing asset-generation workflow, not a new planning pipeline.

### Compiler fixed instructions and transition continuity

The existing Compiler serializes locked rendering/character placement alongside
camera/audio instructions, reserving their word budget. Source-grounded short
Vault wardrobe facts are excluded from repetition; creative prose, hardware
explanations and word limits remain validated. Code-owned style cannot satisfy
a match-cut action check by itself. Mismatched same-scene boundary snapshots use
the existing pipeline's bounded continuity review to distinguish progression
from a genuine contradictory same-instant state; verified results are cached
only for unchanged source inputs. Accepted previews can be reused on instruction
retry without new image generation.

### Focused shot planning

Cinematography keeps the existing Sonnet route and whole-story context, but returns
creative shot decisions rather than repeating fixed reference metadata and camera
summaries. The reference projection retains identity descriptions while omitting
URLs, database IDs and voice fields; authoritative originals remain in the Director.
Structured camera decisions are rendered deterministically using the existing
camera vocabulary. Duration, cast-name and speech-field checks add findings to
the existing QA/FIX loop; semantic continuity and complete dialogue still receive
QA review. The silent 9-second and voiceover 15-second planning caps are retained;
real audio and provider timing checks remain downstream. This extends the existing
core planning/asset-generation milestone, not a second pipeline or model switch.

Recognized local visual QA findings use a field-patch response instead of returning
the whole shot list. Code restricts patch shot IDs and field names, preserves all
other content, and runs full semantic QA again. Structural/dialogue/boundary repairs
retain the existing full-plan correction and dialogue protections. Invalid patches
fail explicitly rather than silently applying broader changes. Existing exact-input
checkpoints and provider system-prefix caching remain in use.

### Shared ad direction

The existing Cinematography call also returns a compact `ad_direction` and
per-shot `shot_direction`; this is not another orchestrator or serial model call.
The approved story, reviewed Clarifier direction, locked references and selected
model remain authoritative. Direction makes the audience takeaway, visual approach,
pacing, shot purpose, performance and product/prop staging explicit. Existing
camera/lighting/action fields are reused rather than duplicated.

Versioned directed shots have an explicit opening and ending instant. Still
requests receive the opening only, not the whole action or final pose; video
compilation receives the action/performance and both endpoints. Existing Compiler
inputs retain the full clip cast; `opening_characters` separately determines which
cast descriptions, Vault images and job-entity references reach the opening still.
A character arriving later must not be inserted into its preview. Existing Compiler
validation and model routing remain in place. Semantic QA receives the approved
story and overall direction. Mechanical direction errors and literal adjacent
framing duplicates join its existing bounded correction loop. Unresolved directed
plans stop before media instead of silently proceeding with residual issues.
Editing action/dialogue invalidates saved direction until that same QA/repair loop
reviews it. Duration trimming cannot delete or rewrite directed story beats.

The plan UI exposes these decisions in expandable, plain-language sections.
Sound direction is explicitly marked `planned_only`: it does not generate, license
or mix a music bed. That is a separate future assembly capability. Historical plans
without versioned direction retain their existing adapters; this change neither
replans them automatically nor regenerates accepted assets on deployment.
This extends the README core planning and real-asset-generation milestone.

### Focused semantic QA (local Ad Director follow-up)

The Director owns creative shot choices. The existing QA call verifies source-scene coverage,
complete speech, feasible performance and semantic continuity; deterministic code owns field,
cast, camera-control and duration checks. Directed QA must provide scene coverage evidence;
missing/invalid evidence is not checkpointed, so Retry can make a real new request. An off-screen
character is not assumed to have vanished. Mechanical camera defects use typed camera-only
patches, with the supported camera vocabulary provided, then the same semantic recheck.
This extends the existing planning/QA milestone; no extra orchestrator or model tier is added.

### Storyboard progress workspace

The job view now uses a responsive storyboard grid, with individual preview reveal and
an enlarge dialog. The existing Director saves a display-only `planning_draft` before QA;
it survives reloads but is never placed in approved `shots`. The existing approval status
gate continues to reject running or failed drafts. Final persistence replaces the draft.
Polling exposes it while the existing event stream reports progress. Paused review keeps
the draft readable and offers the existing Retry action. No extra AI calls are introduced.
This extends the core planning/review UI milestone; it does not reduce provider runtime.
