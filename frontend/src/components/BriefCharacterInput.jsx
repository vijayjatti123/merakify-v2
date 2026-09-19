import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Alert, Avatar, Box, Button, Chip, CircularProgress, Dialog, DialogActions,
  DialogContent, DialogTitle, IconButton, List, ListItemButton, ListItemText, Paper, Stack, TextField, Typography } from "@mui/material";
import { AtSign, Plus, X } from "lucide-react";
import { listCharacters, uploadCharacter, setCharacterVoice, approveCharacter } from "../api/client";
import { activeMentions, mentionPattern, mentionToken, mentionsDiscovered, rememberMentions, resolveTypedMentions, mentionSegments, typedMentions } from "../utils/characterMentions";
import VoicePreviewPicker from "./VoicePreviewPicker";

export default function BriefCharacterInput({ value, onChange, selections, onSelections, disabled, scriptMode, tools }) {
  const input = useRef(null), popup = useRef(null), alive = useRef(true), loadingRef = useRef(false), insertion = useRef(null);
  const [characters, setCharacters] = useState([]), [loading, setLoading] = useState(false);
  const [error, setError] = useState(""), [query, setQuery] = useState(null), [index, setIndex] = useState(0);
  const [tip, setTip] = useState(() => !mentionsDiscovered());
  const [header] = useState(() => !mentionsDiscovered());
  const [adding, setAdding] = useState(false), [name, setName] = useState(""), [description, setDescription] = useState("");
  const [voice, setVoice] = useState(""), [file, setFile] = useState(null), [draft, setDraft] = useState(null);
  const [busy, setBusy] = useState(false), busyRef = useRef(false), [uploadError, setUploadError] = useState("");
  const [savedWarning, setSavedWarning] = useState("");
  const [loaded, setLoaded] = useState(false), [mirrorHost, setMirrorHost] = useState(null), [scrollTop, setScrollTop] = useState(0);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => { setMirrorHost(input.current?.parentElement || null); }, []);
  useEffect(() => { if (value.includes("@") && !loaded && !disabled) refresh(); }, [value, loaded, disabled]);
  useEffect(() => {
    if (!loaded || disabled) return;
    const next = resolveTypedMentions(value, selections, characters);
    if (Object.keys(next).length !== Object.keys(selections).length) {
      onSelections(next); discover();
    }
  }, [value, characters, loaded, selections, disabled, onSelections]);
  useEffect(() => { popup.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" }); }, [index]);
  async function refresh() {
    if (loadingRef.current) return;
    loadingRef.current = true; setLoading(true); setError("");
    try { const data = await listCharacters(); if (alive.current) setCharacters(data.filter(c => c.status === "approved" && c.catalog_status === "customer" && c.display_name?.trim())); }
    catch { if (alive.current) setError("We couldn't load your characters. Try again."); }
    finally { loadingRef.current = false; if (alive.current) { setLoading(false); setLoaded(true); } }
  }
  function discover() { rememberMentions(); setTip(false); }
  function inspect(text, position) {
    const match = text.slice(0, position).match(/(?:^|[^\p{L}\p{M}\p{N}_@])@([\p{L}\p{M}\p{N}_-]*)$/u);
    if (!match) { setQuery(null); return; }
    if (query === null) refresh();
    setQuery({ start: position - match[1].length - 1, end: position, text: match[1] }); setIndex(0);
  }
  const filtered = characters.filter(c => c.display_name.toLocaleLowerCase().replace(/\s+/g, "_")
    .includes((query?.text || "").toLocaleLowerCase()));
  const linked = activeMentions(value, selections);
  const highlighted = Object.keys(linked).length > 0 && mirrorHost;
  const unlinked = [...new Set(typedMentions(value))].filter(token => !linked[token]);
  useEffect(() => {
    input.current?.setCustomValidity(unlinked.length
      ? `Choose a saved character for ${unlinked.join(", ")}, or remove the @ to use ordinary text.` : "");
  }, [value, selections, characters]);
  function openPicker() {
    if (query === null) {
      refresh();
      setQuery({ start: input.current?.selectionStart ?? value.length,
        end: input.current?.selectionEnd ?? value.length, text: "", fromButton: true });
      setIndex(0);
    }
    input.current?.focus();
  }
  function choose(character) {
    const token = mentionToken(character, selections);
    const range = query || insertion.current;
    const start = range?.start ?? value.length, end = range?.end ?? value.length;
    const prefix = (!range || range.fromButton) && start > 0 && !/\s/.test(value[start - 1]) ? " " : "";
    const next = value.slice(0, start) + prefix + token + " " + value.slice(end);
    onSelections({ ...selections, [token]: character }); onChange(next); setQuery(null); insertion.current = null; discover();
    requestAnimationFrame(() => { input.current?.focus(); input.current?.setSelectionRange(start + prefix.length + token.length + 1, start + prefix.length + token.length + 1); });
  }
  function add() { insertion.current = query; setAdding(true); setQuery(null); setUploadError(""); }
  async function save() {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setUploadError("");
    try {
      let current = draft;
      if (!current) { current = await uploadCharacter({ name: name.trim(), description: description.trim(), file }); setDraft(current); }
      if (current.voice_id !== voice) { current = await setCharacterVoice(current.id, voice); setDraft(current); }
      if (current.status !== "approved") current = await approveCharacter(current.id);
      setSavedWarning(current.reference_sheet_error ? "Character saved. Its character-view sheet couldn't be created; the uploaded image is still available." : "");
      setCharacters(prev => [current, ...prev.filter(c => c.id !== current.id)]);
      choose(current); setAdding(false); setDraft(null); setFile(null); setName(""); setDescription(""); setVoice("");
    } catch { setUploadError("We couldn't finish saving this character. Your uploaded draft is kept; try again."); }
    finally { busyRef.current = false; setBusy(false); }
  }
  return <Box data-testid="brief-character-input" onBlur={event => {
    if (!event.currentTarget.contains(event.relatedTarget)) setQuery(null);
  }}>
    <Box sx={{ position: "relative" }}>
    <TextField multiline fullWidth label={scriptMode ? "Your script" : "Your idea"} inputRef={input}
      value={value} disabled={disabled} autoFocus minRows={3}
      placeholder={scriptMode ? "Paste your script… type @ to add a character" : "Describe your video… type @ to add a character"}
      onChange={e => { onChange(e.target.value); inspect(e.target.value, e.target.selectionStart); }}
      onClick={e => inspect(e.target.value, e.target.selectionStart)}
      onKeyDown={e => {
        if (query === null || e.nativeEvent.isComposing) return;
        if (e.key === "Escape") { e.preventDefault(); setQuery(null); }
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          e.preventDefault(); setIndex(i => Math.max(0, Math.min(filtered.length - 1, i + (e.key === "ArrowDown" ? 1 : -1))));
        }
        if (e.key === "Enter") { e.preventDefault(); if (!loading && filtered[index]) choose(filtered[index]); }
      }}
      slotProps={{ inputLabel: { shrink: true }, htmlInput: { "data-testid": "brief-input", "aria-autocomplete": "list", onScroll: e => setScrollTop(e.currentTarget.scrollTop),
        "aria-controls": query !== null ? "character-options" : undefined,
        "aria-activedescendant": query !== null && filtered[index] ? `character-option-${index}` : undefined } }}
      sx={theme => ({ "& textarea": { fontSize: "1.1rem", lineHeight: 1.7, ...(highlighted ? { color: "transparent", WebkitTextFillColor: "transparent", caretColor: (theme.vars || theme).palette.text.primary } : {}) } })} />
    {highlighted && createPortal(<Box aria-hidden="true" data-testid="mention-highlights" sx={{
      position: "absolute", inset: 0, p: "inherit", pointerEvents: "none", overflow: "hidden",
      fontFamily: "inherit", fontSize: "1.1rem", lineHeight: 1.7, letterSpacing: "inherit", color: disabled ? "text.disabled" : "text.primary",
    }}><Box sx={{ whiteSpace: "pre-wrap", overflowWrap: "break-word", transform: `translateY(-${scrollTop}px)` }}>
      {mentionSegments(value, selections).map((part, i) => part.token ? <Box component="span" key={i} data-mention={part.token}
        sx={{ color: "primary.main", bgcolor: "action.selected", borderRadius: "4px" }}>{part.text}</Box> : part.text)}{"\u200b"}
    </Box></Box>, mirrorHost)}
    {query !== null && <Paper ref={popup} data-testid="character-dropdown" elevation={8} sx={{ position: "absolute", top: "100%", left: 0, right: 0, zIndex: 12, mt: 1, p: 1, maxHeight: 260, overflow: "auto" }}>
      {header && <Typography variant="overline" sx={{ px: 2 }}>Your saved characters</Typography>}
      {loading ? <Stack direction="row" gap={1} sx={{ p: 2 }} role="status"><CircularProgress size={18} />Loading characters…</Stack> :
        error ? <Alert severity="error" action={<Button type="button" onClick={refresh}>Retry</Button>}>{error}</Alert> :
        filtered.length ? <List id="character-options" role="listbox" aria-label="Saved characters" dense>
          {filtered.map((c, i) => <ListItemButton component="li" tabIndex={0} role="option" id={`character-option-${i}`} key={c.id}
            aria-selected={i === index} selected={i === index} onMouseDown={e => e.preventDefault()} onClick={() => choose(c)}
            onKeyDown={e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); choose(c); } }}>
            <Avatar src={c.image_url} alt="" sx={{ mr: 1.5, width: 28, height: 28 }} /><ListItemText primary={c.display_name}
              secondary={filtered.filter(other => other.display_name === c.display_name).length > 1 ? `Saved character · ${c.id.slice(0, 6)}` : undefined} />
          </ListItemButton>)}
        </List> : <Box sx={{ p: 2 }} data-testid="characters-empty">
          <Typography>{characters.length ? "No matching characters." : "No characters yet."}</Typography>
          <Button type="button" startIcon={<Plus size={16} />} onClick={add}>Add a character here</Button>
        </Box>}
    </Paper>}
    </Box>
    <Stack spacing={1} sx={{ mt: 1.5 }}>
      {tip && <Stack direction="row" sx={{ alignItems: "center" }} data-testid="mention-tip">
        <Typography variant="body2" color="text.secondary">Tip: type @ to bring in a saved character.</Typography>
        <IconButton type="button" size="small" aria-label="Dismiss character tip" onClick={discover}><X size={16} /></IconButton>
      </Stack>}
      <Stack direction="row" gap={1} sx={{ flexWrap: "wrap" }}>
        <Button type="button" variant="outlined" size="small" disabled={disabled}
          aria-label="Choose character" data-testid="open-character-picker" aria-expanded={query !== null} aria-haspopup="listbox"
          startIcon={<AtSign size={18} />} onMouseDown={e => e.preventDefault()} onClick={openPicker}>Characters</Button>
        <Button type="button" variant="outlined" size="small" disabled={disabled} startIcon={<Plus size={16} />} aria-label="Add character image" onClick={add}>Upload character</Button>
        {tools}
      </Stack>
      {Object.keys(linked).length > 0 && <Stack direction="row" gap={1} sx={{ flexWrap: "wrap" }} aria-live="polite" data-testid="linked-characters">
        {Object.keys(linked).map(token => <Chip key={token} color="success" variant="outlined"
          avatar={<Avatar src={selections[token].image_url} alt="" />} label={`${selections[token].display_name} · linked`} onDelete={disabled ? undefined : () => {
          onChange(value.replace(mentionPattern(token), ""));
          onSelections(Object.fromEntries(Object.entries(selections).filter(([key]) => key !== token)));
        }} />)}
      </Stack>}
      {loaded && !loading && unlinked.length > 0 && <Typography variant="body2" color="warning.main" data-testid="unlinked-mentions">
        {unlinked.join(", ")} isn’t linked yet. Finish the saved name or choose a character from the list; duplicate names need a selection.
      </Typography>}
      {savedWarning && <Alert severity="warning">{savedWarning}</Alert>}
    </Stack>
    <Dialog open={adding} onClose={() => !busy && setAdding(false)} fullWidth maxWidth="sm">
      <DialogTitle>Add a character without leaving your brief</DialogTitle>
      <DialogContent><Stack spacing={2} sx={{ pt: 1 }}>
        <Typography variant="body2">Upload a reference, choose a voice, then save and use this character. Approval also creates the Vault’s character-view sheet.</Typography>
        <TextField label="Display name" helperText="The short name shown in your brief and character picker." value={name} disabled={busy || Boolean(draft)} onChange={e => setName(e.target.value)} slotProps={{ htmlInput: { maxLength: 80 } }} />
        <TextField label="Character description" multiline minRows={2} value={description} disabled={busy || Boolean(draft)} onChange={e => setDescription(e.target.value)} />
        <VoicePreviewPicker value={voice} onChange={setVoice} disabled={busy} />
        <Button component="label" variant="outlined" disabled={busy || Boolean(draft)}>
          {file ? file.name : "Choose reference image"}
          <input data-testid="inline-character-file" type="file" accept="image/*" hidden disabled={busy || Boolean(draft)} onChange={e => setFile(e.target.files?.[0] || null)} />
        </Button>
        {draft && <Typography variant="body2">Uploaded draft saved. Finish approval to use it in this brief.</Typography>}
        {uploadError && <Alert severity="error">{uploadError}</Alert>}
        {busy && <Typography role="status">Saving your character and creating character views…</Typography>}
      </Stack></DialogContent>
      <DialogActions><Button type="button" disabled={busy} onClick={() => setAdding(false)}>Cancel</Button>
        <Button type="button" variant="contained" disabled={busy || !name.trim() || !description.trim() || !voice || !file} onClick={save}>Save to Vault and use</Button></DialogActions>
    </Dialog>
  </Box>;
}
