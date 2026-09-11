import { ArrowLeft, Check, ImagePlus, Loader2, RefreshCw, Sparkles, Upload } from "lucide-react";
import { useEffect, useState } from "react";

import {
  approveCharacter,
  generateCharacter,
  listCharacters,
  setCharacterVoice,
  uploadCharacter,
} from "../api/client";

const SARVAM_VOICES = [
  "shubh", "aditya", "rahul", "rohan", "amit", "dev", "ratan", "varun", "manan", "sumit",
  "kabir", "aayan", "ashutosh", "advait", "anand", "tarun", "sunny", "mani", "gokul", "vijay",
  "mohit", "rehan", "soham", "ritu", "priya", "neha", "pooja", "simran", "kavya", "ishita",
  "shreya", "roopa", "tanya", "shruti", "suhani", "kavitha", "rupali",
];

function voiceLabel(voiceId) {
  return `${voiceId.charAt(0).toUpperCase()}${voiceId.slice(1)} (Sarvam)`;
}

export function isVoiceSelectionSaved(voiceId, savedVoiceId) {
  return Boolean(voiceId && savedVoiceId && voiceId === savedVoiceId);
}

function CharacterImages({ character, compact = false }) {
  return (
    <div className={`vault-character-images${compact ? " is-compact" : ""}`}>
      <figure>
        <img src={character.image_url} alt={`${character.name} approved character`} />
        <figcaption>Character</figcaption>
      </figure>
      {character.reference_sheet_url ? (
        <figure>
          <img src={character.reference_sheet_url} alt={`${character.name} reference sheet`} />
          <figcaption>Reference sheet</figcaption>
        </figure>
      ) : (
        <div className="vault-reference-sheet-empty">
          {character.status === "approved"
            ? "The reference sheet couldn't be generated."
            : "Reference sheet will be generated on approval."}
        </div>
      )}
    </div>
  );
}

export default function CharacterVault({ onBack }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [draft, setDraft] = useState(null);
  const [voiceId, setVoiceId] = useState("");
  const [approvedCharacters, setApprovedCharacters] = useState([]);
  const [busyAction, setBusyAction] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    refreshApproved();
  }, []);

  async function refreshApproved() {
    try {
      setApprovedCharacters(await listCharacters());
    } catch (loadError) {
      setError(loadError.message);
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
    <main className="vault-shell">
      <header className="vault-header">
        <button type="button" onClick={onBack} className="vault-back"><ArrowLeft size={16} /> Director</button>
        <div>
          <p className="eyebrow">Phase 2</p>
          <h1>Character Vault</h1>
          <p>Create reusable visual and voice references. Nothing here changes a job unless you opt in later.</p>
        </div>
      </header>

      <section className="vault-workspace">
        <form className="vault-editor" onSubmit={(event) => event.preventDefault()}>
          <div>
            <p className="eyebrow">Draft character</p>
            <h2>Build a reference</h2>
          </div>

          <label>Name<input value={name} onChange={(event) => setName(event.target.value)} placeholder="e.g. Meera" /></label>
          <label>Description<textarea value={description} onChange={(event) => setDescription(event.target.value)} placeholder="Age, appearance, wardrobe, personality, and distinctive details" rows={5} /></label>

          <div className="vault-image-actions">
            <button type="button" onClick={handleGenerate} disabled={busy} className="vault-primary">
              {busyAction === "generate" ? <Loader2 size={15} className="animate-spin" /> : draft ? <RefreshCw size={15} /> : <Sparkles size={15} />}
              {draft ? "Retry image" : "Generate image"}
            </button>
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
                  Sarvam voice
                  <select value={voiceId} onChange={(event) => setVoiceId(event.target.value)}>
                    <option value="">Choose a voice</option>
                    {SARVAM_VOICES.map((voice) => <option key={voice} value={voice}>{voice}</option>)}
                  </select>
                </label>
                <button type="button" onClick={handleSaveVoice} disabled={busy || !voiceId} className="vault-secondary">
                  {busyAction === "voice" && <Loader2 size={14} className="animate-spin" />} Save voice
                </button>
                {draft.voice_id && (
                  <section
                    aria-label="Character review before approval"
                    style={{ padding: "0.75rem", border: "1px solid #34374c", borderRadius: "0.65rem", background: "#181a27" }}
                  >
                    <p className="eyebrow" style={{ marginBottom: "0.55rem" }}>Review before approval</p>
                    <CharacterImages character={draft} compact />
                    <div style={{ marginTop: "0.65rem" }}>
                      <strong style={{ display: "block", color: "#f3f0e8" }}>{draft.name}</strong>
                      <span style={{ color: "#aaa8bb", fontSize: "0.72rem" }}>
                        Selected voice: {voiceLabel(draft.voice_id)}
                      </span>
                    </div>
                  </section>
                )}
                {draft.voice_id && !voiceSelectionSaved && (
                  <p role="status" className="vault-error">Save your voice selection before approving.</p>
                )}
                <button type="button" onClick={handleApprove} disabled={busy || !voiceSelectionSaved} className="vault-primary">
                  {busyAction === "approve" ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />} Approve character
                </button>
              </div>
            </section>
          )}

          {error && <p className="vault-error">{error}</p>}
        </form>

        <section className="vault-library">
          <div>
            <p className="eyebrow">Approved only</p>
            <h2>Your characters</h2>
          </div>
          {approvedCharacters.length ? (
            <div className="vault-grid">
              {approvedCharacters.map((character) => (
                <article key={character.id} className="vault-character-card">
                  <CharacterImages character={character} />
                  <div>
                    <h3>{character.name}</h3>
                    <p>{character.description}</p>
                    <span>{character.voice_id} · {character.image_source}</span>
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <div className="vault-empty"><ImagePlus size={28} /><p>No approved characters yet.</p></div>
          )}
        </section>
      </section>
    </main>
  );
}
