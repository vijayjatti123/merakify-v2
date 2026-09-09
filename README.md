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
