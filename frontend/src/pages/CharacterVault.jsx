import { ArrowLeft, Check, ImagePlus, Loader2, RefreshCw, Sparkles, Upload } from "lucide-react";
import { useEffect, useState } from "react";
import { Alert, Box, Button, Card, CardContent, CardMedia, Chip, Skeleton, Typography, TextField } from "@mui/material";
import ActionProgress from "../components/ActionProgress";
import { friendlyMessage } from "../utils/presentation";

import {
  approveCharacter,
  generateCharacter,
  listCharacters,
  setCharacterVoice,
  uploadCharacter,
} from "../api/client";

import { SARVAM_VOICES } from "../utils/voices";

function voiceLabel(voiceId) {
  return `${voiceId.charAt(0).toUpperCase()}${voiceId.slice(1)}`;
}

export function isVoiceSelectionSaved(voiceId, savedVoiceId) {
  return Boolean(voiceId && savedVoiceId && voiceId === savedVoiceId);
}

function CharacterImages({ character, compact = false }) {
  return (
    <div className={`vault-character-images${compact ? " is-compact" : ""}`}>
      <figure>
        <img src={character.image_url} alt={`${character.display_name} approved character`} />
        <figcaption>Character</figcaption>
      </figure>
      {character.reference_sheet_url ? (
        <figure>
          <img src={character.reference_sheet_url} alt={`${character.display_name} character views`} />
          <figcaption>Character views</figcaption>
        </figure>
      ) : (
        <div className="vault-reference-sheet-empty">
          {character.status === "approved"
            ? "The character views couldn't be generated."
            : "Character views will be generated on approval."}
        </div>
      )}
    </div>
  );
}

function VaultImage({ src, label }) {
  const [state, setState] = useState("loading");
  useEffect(() => setState("loading"), [src]);
  return <Box sx={{ position: "relative", bgcolor: "var(--mui-palette-action-hover)", minHeight: 160 }}>
    {state === "loading" && <Skeleton variant="rectangular" height={200} aria-label={`Loading ${label}`} />}
    {state === "error" ? <Alert severity="warning">Image unavailable. Try Refresh references.</Alert> :
      <CardMedia component="img" image={src} alt={label} onLoad={() => setState("loaded")} onError={() => setState("error")}
        sx={{ width: "100%", height: 240, objectFit: "contain", display: state === "loaded" ? "block" : "none" }} />}
  </Box>;
}

export default function CharacterVault({ onBack }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [draft, setDraft] = useState(null);
  const [voiceId, setVoiceId] = useState("");
  const [approvedCharacters, setApprovedCharacters] = useState([]);
  const [loading, setLoading] = useState(true);
  const [libraryError, setLibraryError] = useState("");
  const [busyAction, setBusyAction] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    refreshApproved();
    const timer = setInterval(refreshApproved, 5 * 60 * 1000);
    window.addEventListener("focus", refreshApproved);
    return () => { clearInterval(timer); window.removeEventListener("focus", refreshApproved); };
  }, []);

  async function refreshApproved() {
    setLoading(true); setLibraryError("");
    try {
      setApprovedCharacters(await listCharacters());
    } catch (loadError) {
      setLibraryError(loadError.message);
    } finally {
      setLoading(false);
    }
  }

  function requireDescription() {
    if (!name.trim() || !description.trim()) {
      setError("Add a character name and description first.");
      return false;
    }
    return true;
  }

  async function handleGenerate() {
    if (!requireDescription()) return;
    setBusyAction("generate");
    setError("");
    try {
      const character = await generateCharacter({
        name: name.trim(),
        description: description.trim(),
        characterId: draft?.id,
      });
      setDraft(character);
      setVoiceId(character.voice_id || voiceId);
    } catch (generateError) {
      setError(generateError.message);
    } finally {
      setBusyAction("");
    }
  }

  async function handleUpload(event) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file || !requireDescription()) return;
    setBusyAction("upload");
    setError("");
    try {
      const character = await uploadCharacter({
        name: name.trim(),
        description: description.trim(),
        file,
        characterId: draft?.id,
      });
      setDraft(character);
      setVoiceId(character.voice_id || voiceId);
    } catch (uploadError) {
      setError(uploadError.message);
    } finally {
      setBusyAction("");
    }
  }

  async function handleSaveVoice() {
    if (!draft || !voiceId) return;
    setBusyAction("voice");
    setError("");
    try {
      setDraft(await setCharacterVoice(draft.id, voiceId));
    } catch (voiceError) {
      setError(voiceError.message);
    } finally {
      setBusyAction("");
    }
  }

  async function handleApprove() {
    if (!draft) return;
    setBusyAction("approve");
    setError("");
    try {
      const character = await approveCharacter(draft.id);
      setApprovedCharacters((current) => [character, ...current.filter((item) => item.id !== character.id)]);
      setDraft(null);
      setName("");
      setDescription("");
      setVoiceId("");
    } catch (approveError) {
      setError(approveError.message);
    } finally {
      setBusyAction("");
    }
  }

  const busy = Boolean(busyAction);
  const voiceSelectionSaved = isVoiceSelectionSaved(voiceId, draft?.voice_id);

  return (
    <Box component="section" className="vault-shell">
      <header className="vault-header">
        <Button type="button" onClick={onBack} className="vault-back"><ArrowLeft size={16} /> Back to video</Button>
        <div>
          <p className="eyebrow">Your cast</p>
          <h1>Character Vault</h1>
          <p>Give your stories a familiar face. Save characters and voices to use in your videos.</p>
        </div>
      </header>

      <section className="vault-workspace">
        <Card component="form" className="vault-editor" onSubmit={(event) => event.preventDefault()}>
          <div>
            <p className="eyebrow">Draft character</p>
            <h2>Create a character</h2>
          </div>

          <TextField fullWidth label="Display name" value={name} onChange={(event) => setName(event.target.value)} placeholder="e.g. Meera" />
          <TextField fullWidth multiline label="Description" value={description} onChange={(event) => setDescription(event.target.value)} placeholder="Age, appearance, wardrobe, personality, and distinctive details" minRows={4} />
          {busy && <ActionProgress label={{ generate: "Creating your character image…", upload: "Uploading your character image…", voice: "Saving your voice choice…", approve: "Saving your character and creating character views…" }[busyAction]} />}

          <div className="vault-image-actions">
            <Button type="button" onClick={handleGenerate} disabled={busy} className="vault-primary">
              {busyAction === "generate" ? <Loader2 size={15} className="animate-spin" /> : draft ? <RefreshCw size={15} /> : <Sparkles size={15} />}
              {draft ? "Retry image" : "Generate image"}
            </Button>
            <label className="vault-upload">
              {busyAction === "upload" ? <Loader2 size={15} className="animate-spin" /> : <Upload size={15} />}
              Upload image
              <input type="file" accept="image/*" disabled={busy} onChange={handleUpload} />
            </label>
          </div>

          {draft && (
            <section className="vault-draft-card">
              <img src={draft.image_url} alt={`${draft.name} character reference`} />
              <div className="vault-draft-details">
                <span className="vault-source">{draft.image_source}</span>
                <label>
                  Character voice
                  <select value={voiceId} onChange={(event) => setVoiceId(event.target.value)}>
                    <option value="">Choose a voice</option>
                    {SARVAM_VOICES.map((voice) => <option key={voice} value={voice}>{voice}</option>)}
                  </select>
                </label>
                <Button type="button" onClick={handleSaveVoice} disabled={busy || !voiceId} className="vault-secondary">
                  {busyAction === "voice" && <Loader2 size={14} className="animate-spin" />} Save voice
                </Button>
                {draft.voice_id && (
                  <section
                    aria-label="Character review before approval"
                    style={{ padding: "0.75rem", border: "1px solid var(--mui-palette-divider)", borderRadius: "0.65rem", background: "var(--mui-palette-background-paper)" }}
                  >
                    <p className="eyebrow" style={{ marginBottom: "0.55rem" }}>Review before approval</p>
                    <CharacterImages character={draft} compact />
                    <div style={{ marginTop: "0.65rem" }}>
                      <strong style={{ display: "block", color: "var(--mui-palette-text-primary)" }}>{draft.name}</strong>
                      <span style={{ color: "var(--mui-palette-text-secondary)", fontSize: "0.72rem" }}>
                        Selected voice: {voiceLabel(draft.voice_id)}
                      </span>
                    </div>
                  </section>
                )}
                {draft.voice_id && !voiceSelectionSaved && (
                  <p role="status" className="vault-error">Save your voice selection before approving.</p>
                )}
                <Button type="button" onClick={handleApprove} disabled={busy || !voiceSelectionSaved} className="vault-primary">
                  {busyAction === "approve" ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />} Approve character
                </Button>
              </div>
            </section>
          )}

          {error && <Alert severity="error">{friendlyMessage(error, "Your character could not be saved. Please try again.")}</Alert>}
        </Card>

        <Card component="section" className="vault-library">
          <div>
            <p className="eyebrow">Approved only</p>
            <h2>Your characters</h2>
          </div>
          <Button size="small" disabled={loading} onClick={refreshApproved}>Refresh references</Button>
          {libraryError && <Alert severity="error">{friendlyMessage(libraryError, "Your characters could not be loaded. Please refresh.")}</Alert>}
          {loading && !approvedCharacters.length ? <Box sx={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 2 }}>
            {[0, 1, 2, 3].map((index) => <Skeleton key={index} variant="rounded" height={280} aria-label="Loading character" />)}
          </Box> : approvedCharacters.length ? (
            <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", md: "repeat(2, minmax(0, 1fr))" }, gap: 2 }}>
              {approvedCharacters.map((character) => (
                <Card key={character.id} component="article" variant="outlined" sx={{ minWidth: 0 }}>
                  <VaultImage src={character.image_url} label={`${character.display_name} approved character`} />
                  <CardContent>
                    <Typography component="h3" variant="h6" sx={{ overflowWrap: "anywhere" }}>{character.display_name}</Typography>
                    <Typography variant="body2" color="text.secondary" sx={{ my: 1 }}>{character.description}</Typography>
                    <Chip size="small" label={`${character.voice_id} · ${character.image_source}`} />
                  </CardContent>
                  {character.reference_sheet_url && <Box sx={{ px: 2, pb: 2 }}>
                    <Typography variant="caption">Character views</Typography>
                    <VaultImage src={character.reference_sheet_url} label={`${character.display_name} character views`} />
                  </Box>}
                </Card>
              ))}
            </Box>
          ) : (
            !libraryError && <div className="vault-empty"><ImagePlus size={28} /><p>No approved characters yet.</p></div>
          )}
        </Card>
      </section>
    </Box>
  );
}
