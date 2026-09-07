# Merakify Core — repository guide for coding agents

## What this repo is

This is a from-scratch rebuild of one thing only: brief in → Director Agent
pipeline runs autonomously → shot list out. It intentionally does not contain
screenwriting tools, producer/budget/crew tools, a music studio, a YouTube
series tool, or research/pitch-deck generators. Do not add features from that
category here, even if asked to "port" something from the old platform —
raise it as a separate-product question instead of building it into this
core surface.

## Architecture

- `backend/app/main.py` — FastAPI entry point, all routes under `/api`.
- `backend/app/routes/jobs.py` — the entire API surface: create a job, poll
  it, or stream its progress over SSE. Keep it that way. New capability
  extends the pipeline in `director.py`, not a new parallel route file.
- `backend/app/agents/director.py` — the orchestrator. There must only ever
  be ONE pipeline implementation. The old codebase had four competing
  ad-creation implementations (alpha/v3/cinematic/cinematic_v2) built up over
  time without removing earlier versions — that is the single most important
  mistake not to repeat here. If a task asks you to add a new capability,
  extend the existing sequence of agent calls; do not create
  `director_v2.py` or a second orchestrator next to it.
- `backend/app/agents/prompts.py` — one system prompt per specialist agent.
  Keep each agent's domain knowledge (film grammar, narrative structure)
  encoded here as explicit rules, not left implicit in a generic prompt.
- `backend/app/agents/llm_client.py` — model routing lives here: cheap/fast
  model for classification, stronger model for reasoning-heavy steps
  (cinematography, QA). Preserve this split when adding new agent calls;
  don't default new steps to the expensive model without a reason.
- `backend/app/services/job_service.py` — all DB writes for jobs and agent
  events. Route persistence through here, not ad hoc queries in route files.
- `frontend/` — Vite + React, two screens (`NewJob`, `JobView`). Keep it to
  the minimum screens needed for the core loop; don't add a dashboard of
  tools as separate pages.

## The one pattern that must be preserved

The Continuity QA Agent inspects the Cinematography Agent's output and can
send it back for revision with no human approving that step
(`director.py`, the `if not qa.get("approved")` block). This self-correction
loop is what makes the system agentic rather than a single generative call
with a UI around it — it is core to the product's pitch, not incidental.
Any new pipeline step that could produce an error (a generated asset that
doesn't match its reference, a rendered shot that violates continuity)
should get the same treatment: a checking step that can autonomously trigger
a retry, not just a step that reports a problem for a human to fix.

## Indic dialogue audio: no lip-sync step, by decision — don't silently reintroduce one

Dialogue-shot audio (Sarvam/ElevenLabs) is passed directly to the video model
as reference audio; there is deliberately no separate lip-sync pass (Sync
Labs/HeyGen) in the default pipeline. This trades a known accuracy risk for
real simplicity, and the risk is made visible rather than hidden:
`_attach_voice_refs` in `director.py` sets `experimental_audio_sync` and
`audio_sync_note` on every dialogue shot, and the frontend shows it as a
warning. If you're asked to improve dialogue-shot quality, do not add a
lip-sync step back in without being asked to — that reverses a deliberate
product decision, not an oversight. The correct move if accuracy turns out to
be bad is to say so and ask, the same way any other reversal of a documented
decision here should be flagged rather than silently made.

## Provider and infrastructure rules

- Every AI provider call uses a direct API key from `backend/.env`. Never
  route a call through a third-party universal-key gateway or proxy,
  regardless of how convenient it looks for auth or storage. The old
  codebase's file storage and Google login were both silently proxied
  through Emergent's infrastructure this way, which became a real migration
  cost — don't reintroduce that pattern here for any provider.
- Object storage, when added, goes directly to S3 via `boto3`. Auth, when
  added, is a standard OAuth flow with our own `client_id`/`client_secret` —
  never a managed third-party auth proxy.
- `DATABASE_URL` is the only thing that should change between local dev
  (SQLite) and Railway (Postgres). Don't hardcode either database engine
  elsewhere in the code.
- `backend/.env.example` is the authoritative list of every environment
  variable this project uses, real names, kept current as providers are
  added. Read it before adding a new provider integration rather than
  inventing a variable name — and add your new variable to it, with a
  comment, in the same change that starts using it.

## Local setup

1. Backend: `cd backend && python -m venv venv && source venv/bin/activate
   && pip install -r requirements.txt && cp .env.example .env` — fill in
   `ANTHROPIC_API_KEY`. Run with `uvicorn app.main:app --reload --port 8000`.
2. Frontend: `cd frontend && npm install && cp .env.example .env && npm run
   dev`.
3. Verify: `curl -X POST localhost:8000/api/jobs -H "Content-Type:
   application/json" -d '{"brief":"a short test ad"}'` should return a job id
   immediately, and `GET /api/jobs/{id}` should show `status: "running"` then
   `"done"` within roughly 30-60 seconds.

## Working agreements

- Never commit `.env` files, API keys, or real credentials of any kind —
  including in test fixtures, seed scripts, or markdown notes. The old repo
  had a real admin password sitting in a committed markdown file; treat that
  as the standard to actively check for, not just avoid repeating.
- After backend changes, verify `app.main` still imports and run
  `uvicorn app.main:app` locally at least once before considering a task
  done — don't rely on the diff looking correct.
- After frontend changes, run `npm run build` and confirm it succeeds.
- Keep task scope narrow. If a task description implies touching more than
  one of `routes/`, `agents/`, `services/`, or `frontend/src/pages/`, flag
  that before proceeding rather than expanding the change quietly.
- Write a short note in the PR/commit description naming which of the "next
  milestone" TODOs in `README.md` a change addresses, if any.

## Code review rules

- Flag any new provider call that doesn't use a direct API key from env.
- Flag any new file that duplicates an existing pipeline stage rather than
  extending it (a second `director.py`-shaped file is always wrong here).
- Flag hardcoded credentials, permissive default auth, or logging of full
  prompts/user briefs at a level that would land in shared logs.
- Flag any step added to the pipeline that can fail silently — every agent
  call should either succeed, raise (caught by `director.py`'s existing
  handler), or go through an explicit QA-style check.
