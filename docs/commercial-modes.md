# Commercial formats — phased implementation and release gates

## Phase 1: shared intake and handoff (implemented locally)

Four choices in Create: Character Commercial (the legacy default), Product
Commercial, CGI Commercial and UGC Commercial. No additional page or wizard.
An idea/script remains the main input. Optional message and treatment fields are
visible; audience, CTA, exclusions and speech preference sit in a disclosure.
Duration, aspect ratio and language stay visible. Less frequent style, quality
and model controls share one disclosure. CGI initially selects 3D / CGI, with
per-mode style choices retained while switching. Existing jobs remain Character.

The typed `ad_type` and `ad_brief` are persisted and returned by the real jobs
API, including retries. Product and CGI require approved product references.
UGC services do not. Failed-plan recovery now copies immutable product snapshots.
Clarification receives the same context, does not re-ask populated structured
topics, and cannot apply an old refinement after the commercial context changes.

The existing Director receives format-specific instructions with its current
calls. There is no extra orchestrator, planning round trip, provider substitution
or external lip-sync pass. Code checks explicit speech preferences before paid
preview approval. User story review and existing media verification remain.
Opening-frame instructions specialize the existing still adapter/checker and
exclude later CGI transformations. This is not proof of generated media quality.

## Phase 2: Product release audit (remaining)

Use one approved product through story, edited/approved plan, preview, video and
assembly. Verify geometry, label, reflections, scale and product/hand contact.
Check rejection recovery preserves the reference and accepted sibling assets.
Record actual provider durations, processing time, charges and all media files.
Existing ProductPicker supports choosing an original or prepared product image;
the new mode does not silently regenerate or approve the product master.

## Phase 3: UGC release audit (remaining)

Test both physical-product and service stories. Verify creator identity, explicit
speaker, approved voice/dialogue, hand contact and natural framing. Test narration
separately from on-screen speech. Never present synthetic performance as verified
customer testimony. Direction supports these intentions; actual output still
requires inspection and speech verification.

## Phase 4: CGI release audit and advanced control (remaining)

Audit a simple reveal/transformation with a locked product and a separate
opening state. Verify the effect does not destroy the package/brand or appear in
the opening prematurely. Current generation reuses existing model adapters;
there is no new end-frame-conditioned model route, 3D simulation or physics engine.
Only introduce additional keyframe controls after their actual provider contract
and output have been tested. This mode promises AI CGI-style direction only.

## Phase 5: finishing and release (remaining)

Do not call planned music cues rendered music. Deterministic branded end-card,
CTA/logo overlays, music mixing and per-mode final export review require their
own implementation/audit if not supported by the current assembly contract.
Keep existing five-second shot floor and short-ad continuous-shot preference.
Benchmark one complete ad per mode before claiming production readiness. Do not
deploy mode cards as evidence that these release gates have passed.

## Current evidence

- 65 targeted tests pass: commercial intake/API/migration/retry, Clarifier,
  user Director review, still generation and planning constraints.
- Actual FastAPI requests use isolated SQLite; provider calls are mocked or
  the worker is intentionally not started. These are not live generation tests.
- Frontend production build passes (existing >500 kB chunk warning remains).
- Actual Uvicorn startup and `/api/health` succeed with an empty isolated DB.
- Browser checks: four choices, mode-specific copy, product-required state,
  service UGC enabled without a product, per-mode draft retention, CGI style
  default, mobile viewport 390 px with no horizontal overflow.
- Two broader `test_still_recovery` assertions fail and reproduce with the HEAD
  preview adapter: duplicate failed progress event; legacy success fixture does
  not reach its mocked video-provider call. These remain a separate release gate.
- UI screenshots: sibling `commercial-modes/` evidence directory outside Git.
- No paid media generation, production writes, commit or deployment in this phase.

README milestone: extends the existing brief-to-Director/asset-generation path;
does not add a separate tool or orchestrator.
