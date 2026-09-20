import { useEffect, useRef, useState } from "react";
import { Alert, Box, Button, Card, Chip, LinearProgress, Stack, TextField, Typography } from "@mui/material";
import { Sparkles } from "lucide-react";
import { clarifierRequest } from "../api/clarifier";

const topicLabels = { product: "Product benefit", audience: "Audience", outcome: "Viewer takeaway", execution: "Visual execution",
  script_clarity: "Script details", tone: "Tone", differentiator: "Main selling point", constraints: "Must-haves and exclusions" };

// Input changes must never silently discard a conversation.
export default function ClarifierPanel({ brief, knownFields, onUse, prepareRefined = text => text, inputMode = "idea", productIds = [], adType = "character", adBrief = {}, disabled = false }) {
  const [appliedBrief, setAppliedBrief] = useState(null);
  const [source, setSource] = useState(null);
  const [generation, setGeneration] = useState(0);
  const context = JSON.stringify([knownFields, inputMode, productIds, adType, adBrief]);
  const signature = JSON.stringify([brief, context]);
  const eligible = brief.trim().length >= 20 && brief.trim().split(/\s+/).length >= 4;
  const changed = source && source.signature !== signature;
  const inputs = source || { brief, knownFields, inputMode, productIds, adType, adBrief, signature };
  const inputContext = JSON.stringify([inputs.knownFields, inputs.inputMode, inputs.productIds, inputs.adType, inputs.adBrief]);
  return <Box hidden={disabled || signature === appliedBrief || (!eligible && !source)}>
    {changed && <Alert severity="info" data-testid="clarifier-inputs-changed" sx={{ mb: 2 }}>
      Your brief or settings changed. This conversation remains editable and keeps using the inputs it started with.
      Start again only if you want the questions to use your updated inputs.
      <Button type="button" data-testid="clarifier-restart" disabled={!eligible}
        onClick={() => { setSource(null); setGeneration(n => n + 1); }}>Start again with updated inputs</Button>
    </Alert>}
    <Conversation key={generation} {...inputs} paused={disabled}
      prepareRefined={prepareRefined}
      onStart={() => setSource(inputs)}
      onUse={(text, row) => {
        setAppliedBrief(JSON.stringify([inputs.inputMode === "script" ? inputs.brief : text, inputContext]));
        onUse(text, row, { context: inputContext, inputMode: inputs.inputMode, brief: inputs.brief });
      }} />
  </Box>;
}

function Conversation({ brief, knownFields, onUse, prepareRefined, inputMode, productIds, adType, adBrief, onStart, paused }) {
  const [session, setSession] = useState(null);
  const [answer, setAnswer] = useState("");
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const [error, setError] = useState(null);
  const alive = useRef(true), inFlight = useRef(false), latest = useRef(null), accepted = useRef(false);
  const answerInput = useRef(null), editor = useRef(null), heading = useRef(null);
  const cancel = (row) => row && clarifierRequest(`/${row.session_id}/cancel`, { revision: row.revision }).catch(() => {});
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);
  const pending = !session?.refined_prompt && session?.turns?.find(t => t.answer === null);
  const degraded = session?.status === "degraded";
  const highConfidence = session?.confidence >= .8 && session?.status !== "degraded";
  useEffect(() => {
    if (!busy && !dismissed) {
      if (pending) answerInput.current?.focus();
      else if (session) heading.current?.focus();
    }
  }, [busy, pending?.question, session?.refined_prompt, dismissed]);

  function acceptSnapshot(row) {
    latest.current = row; setSession(row); setDraft(row.refined_prompt || ""); setAnswer("");
  }
  async function run(work) {
    if (inFlight.current || !alive.current || paused) return;
    inFlight.current = true; setBusy(true); setError(null);
    try {
      const row = await work();
      if (!alive.current) return;
      acceptSnapshot(row);
    } catch (e) {
      if (alive.current) setError({ message: e.name === "AbortError"
        ? "This is taking longer than expected. Reload the conversation or use your original brief."
        : e.message, status: e.status });
    } finally { inFlight.current = false; if (alive.current) setBusy(false); }
  }
  const post = (row, action, extra = {}) => clarifierRequest(`/${row.session_id}/${action}`, { revision: row.revision, ...extra });
  function leave() {
    alive.current = false; setDismissed(true);
    if (!inFlight.current) cancel(latest.current);
  }
  async function refine() {
    await run(async () => {
      let row = latest.current;
      if (row.refined_prompt && draft !== row.refined_prompt) row = await post(row, "edit", { refined_prompt: draft });
      latest.current = row;
      return post(row, "refine");
    });
  }
  async function useRefined() {
    if (inFlight.current || !draft.trim()) return;
    await run(async () => {
      const prepared = prepareRefined(draft);
      const row = prepared === latest.current.refined_prompt ? latest.current
        : await post(latest.current, "edit", { refined_prompt: prepared });
      if (alive.current) { accepted.current = true; onUse(row.refined_prompt, row); setDismissed(true); }
      return row;
    });
  }
  if (dismissed) return null;
  return <Card component="section" aria-label="Refine your idea" data-testid="clarifier-panel"
    onKeyDown={e => { if (e.key === "Enter") e.stopPropagation(); }}
    sx={{ p: { xs: 2, sm: 3 }, bgcolor: "background.paper",
      ...(degraded && { borderLeft: "5px solid", borderLeftColor: "warning.main" }) }}>
    <Stack component="fieldset" disabled={paused} spacing={2} sx={{ border: 0, p: 0, m: 0, minWidth: 0 }}>
      <Typography ref={heading} tabIndex={-1} variant="h6" data-testid="clarifier-heading">A little clarity, a stronger idea</Typography>
      {degraded && <Chip size="small" color="warning" variant="outlined" sx={{ alignSelf: "flex-start" }}
        data-testid="clarifier-general-label" label={session.refined_prompt ? "General guidance" : "General question"} />}
      {busy && <Box role="status" data-testid="clarifier-loading"><Typography variant="body2" sx={{ mb: 1 }}>Thinking through your idea…</Typography><LinearProgress /></Box>}
      {error && <Alert severity="error" data-testid="clarifier-error" action={session && <Button type="button" data-testid="clarifier-reload" disabled={busy}
        onClick={() => run(() => clarifierRequest(`/${session.session_id}`))}>Reload</Button>}>{error.message}</Alert>}
      {session?.status === "degraded" && <Alert severity="warning" data-testid="clarifier-degraded">
        We couldn't fully process this right now — {session.refined_prompt ? "this draft uses the details you provided." : "here are some general questions instead."}
      </Alert>}
      {!!session?.assessment?.unverified?.length && <Alert severity="info" data-testid="clarifier-unverified">Some details couldn't be confirmed from your input. We'll ask rather than assume.</Alert>}
      {!session && <Stack spacing={2} data-testid="clarifier-initial">
        <Typography color="text.secondary">We'll read your {inputMode === "script" ? "script" : "idea"} and ask only about missing production decisions. Up to three questions; skip whenever you like.</Typography>
        <Stack direction="row" spacing={1}><Button type="button" variant="contained" startIcon={<Sparkles size={18} />} data-testid="clarifier-start" disabled={busy}
          onClick={() => { onStart(); return run(() => clarifierRequest("/start", { raw_brief: brief, input_mode: inputMode, product_ids: productIds, ad_type: adType, ad_brief: adBrief, known_fields: Object.fromEntries(Object.entries(knownFields).filter(([,v]) => v?.trim())) })); }}>Refine with AI</Button>
          <Button type="button" data-testid="clarifier-skip" onClick={leave}>Skip</Button></Stack>
      </Stack>}
      {session?.assessment?.understanding && <Box data-testid="clarifier-understanding"><Typography variant="subtitle2">Here's what we understand</Typography><Typography variant="body2" color="text.secondary">{session.assessment.understanding}</Typography></Box>}
      {!!session?.turns?.length && <Stack component="ol" spacing={1.5} sx={{ m: 0, pl: 2.5 }} data-testid="clarifier-history">
        {session.turns.filter(t => t.answer !== null).map((t, i) => <Box component="li" key={i}>
          <Typography variant="body2" fontWeight={600}>{t.question}</Typography><Typography variant="body2" color="text.secondary" sx={{ whiteSpace: "pre-wrap" }}>{t.answer}</Typography>
        </Box>)}
      </Stack>}
      {pending && <Stack spacing={2} data-testid="clarifier-question">
        <Typography variant="overline" aria-live="polite">Question {session.turns.length} of up to {session.max_questions || 3}</Typography>
        {!degraded && pending.source === "fallback" && <Chip size="small" label="General question" sx={{ alignSelf: "flex-start" }} />}
        <Typography fontWeight={600} data-testid="clarifier-question-text">{pending.question}</Typography>
        <TextField inputRef={answerInput} fullWidth size="small" label="Your answer" value={answer} disabled={busy}
          onChange={e => setAnswer(e.target.value)} slotProps={{ htmlInput: { "data-testid": "clarifier-answer", maxLength: 8000 } }}
          onKeyDown={e => { if (e.key === "Enter" && !e.nativeEvent.isComposing) { e.preventDefault(); if (answer.trim()) run(() => post(latest.current, "answer", { answer: answer.trim() })); } }} />
        {Array.isArray(pending.options) && <Stack direction="row" useFlexGap sx={{ flexWrap: "wrap", gap: 1 }} data-testid="clarifier-options">
          {pending.options.map((option, i) => { const text = typeof option === "string" ? option : option.label; return <Chip key={i} label={text} disabled={busy} clickable
            data-testid={`clarifier-option-${i}`} aria-pressed={answer === text} color={answer === text ? "primary" : "default"} onClick={() => setAnswer(text)} />; })}
        </Stack>}
        <Button type="button" variant="contained" data-testid="clarifier-submit" disabled={busy || !answer.trim()}
          onClick={() => run(() => post(latest.current, "answer", { answer: answer.trim() }))}>Continue</Button>
      </Stack>}
      {session && !session.refined_prompt && <Stack direction={{ xs: "column", sm: "row" }} spacing={1} data-testid="clarifier-escapes">
        <Button type="button" data-testid="clarifier-refine-now" disabled={busy} onClick={refine}>{pending ? "Skip this question and refine now" : "Refine now"}</Button>
        <Button type="button" data-testid="clarifier-original" onClick={leave}>Use my original brief</Button>
      </Stack>}
      {session?.refined_prompt && <Stack spacing={2} data-testid="clarifier-refined">
        {highConfidence ? <Alert severity="success" data-testid="clarifier-ready-high">Your idea has a clear direction. Review your brief before using it.</Alert>
          : <Alert severity="warning" data-testid="clarifier-ready-low"><strong>More detail would help.</strong> This is our best attempt — feel free to add more detail yourself.
            {session.turns.length >= (session.max_questions || 3) && " We've reached the question limit, but some details are still uncertain."}</Alert>}
        {!!session.assessment?.unresolved?.length && <Typography variant="body2" color="text.secondary" data-testid="clarifier-unresolved">
          Still open: {session.assessment.unresolved.map(topic => topicLabels[topic] || "Creative detail").join(", ")}. You can add these details to the draft below.
        </Typography>}
        <Stack spacing={2} data-testid="clarifier-result-box" sx={{ borderLeft: "5px solid",
          borderLeftColor: highConfidence ? "success.main" : "warning.main", pl: 2, py: 1 }}>
          <Chip size="small" variant="outlined" color={highConfidence ? "success" : "warning"}
            sx={{ alignSelf: "flex-start" }} data-testid="clarifier-result-badge"
            label={highConfidence ? "Clear direction" : "Needs more detail"} />
          {inputMode === "script" && <Typography variant="body2">Your script and dialogue stay unchanged. These notes guide how we plan and produce it.</Typography>}
          <TextField inputRef={editor} multiline fullWidth minRows={5} label={inputMode === "script" ? "Your production direction" : "Your refined brief"} value={draft} disabled={busy}
            onChange={e => setDraft(e.target.value)} slotProps={{ htmlInput: { "data-testid": "clarifier-draft", maxLength: 20000 } }} />
        </Stack>
        <Stack direction="row" useFlexGap sx={{ flexWrap: "wrap", gap: 1 }}>
          <Button type="button" data-testid="clarifier-regenerate" disabled={busy || !draft.trim()} onClick={refine}>Regenerate</Button>
          <Button type="button" data-testid="clarifier-edit" disabled={busy} onClick={() => editor.current?.focus()}>Edit</Button>
          <Button type="button" data-testid="clarifier-cancel" onClick={leave}>Cancel</Button>
          <Button type="button" data-testid="clarifier-use" variant="contained" disabled={busy || !draft.trim()} onClick={useRefined}>{inputMode === "script" ? "Use production direction" : "Use refined brief"}</Button>
        </Stack>
      </Stack>}
    </Stack>
  </Card>;
}
