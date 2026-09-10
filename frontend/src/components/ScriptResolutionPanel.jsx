import { ArrowLeft, Check, ImagePlus, Loader2, Sparkles, Upload } from "lucide-react";
import { useMemo, useState } from "react";

import {
  approveCharacter,
  generateCharacter,
  setCharacterVoice,
  uploadCharacter,
} from "../api/client";
import { buildResolutionItems, buildResolutionMap } from "../utils/scriptResolution";

const SARVAM_VOICES = [
  "shubh", "aditya", "rahul", "rohan", "amit", "dev", "ratan", "varun", "manan", "sumit",
  "kabir", "aayan", "ashutosh", "advait", "anand", "tarun", "sunny", "mani", "gokul", "vijay",
  "mohit", "rehan", "soham", "ritu", "priya", "neha", "pooja", "simran", "kavya", "ishita",
  "shreya", "roopa", "tanya", "shruti", "suhani", "kavitha", "rupali",
];

function CharacterResolutionRow({ item, characters, resolution, onResolve, onApproved }) {
  const [choice, setChoice] = useState("");
  const [name, setName] = useState(item.displayName);
  const [description, setDescription] = useState("");
  const [draft, setDraft] = useState(null);
  const [voiceId, setVoiceId] = useState("");
  const [busyAction, setBusyAction] = useState("");
  const [error, setError] = useState("");
  const busy = Boolean(busyAction);

  function handleChoice(event) {
    const value = event.target.value;
    setChoice(value);
    setError("");
    if (value === "invent") {
      onResolve({ mode: "invent", name: item.displayName });
      return;
    }
    if (value.startsWith("vault:")) {
      const character = characters.find(({ id }) => `vault:${id}` === value);
      onResolve({ mode: "vault", name: item.displayName, character });
      return;
    }
    onResolve(null);
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
      onApproved(character);
      setChoice(`vault:${character.id}`);
      onResolve({ mode: "vault", name: item.displayName, character });
    } catch (approveError) {
      setError(approveError.message);
    } finally {
      setBusyAction("");
    }
  }

  return (
    <article className="resolution-row" data-resolution-state={resolution ? "resolved" : "unresolved"}>
      <div className="resolution-row-heading">
        <div><span>Character</span><h3>{item.displayName}</h3></div>
        <span className={`resolution-status ${resolution ? "resolution-status--done" : ""}`}>
          {resolution ? "Resolved" : "Needs a choice"}
        </span>
      </div>
      <label>
        Resolution
        <select aria-label={`Resolve character ${item.displayName}`} value={choice} onChange={handleChoice}>
          <option value="">Choose how to resolve</option>
          {characters.map((character) => (
            <option key={character.id} value={`vault:${character.id}`}>Vault: {character.name}</option>
          ))}
          <option value="create">Create a new character here</option>
          <option value="invent">Let the AI invent this one</option>
        </select>
      </label>

      {choice === "create" && (
        <section className="inline-character-flow" aria-label={`Create ${item.displayName} inline`}>
          <label>Name<input value={name} onChange={(event) => setName(event.target.value)} /></label>
          <label>Description<textarea rows={3} value={description} onChange={(event) => setDescription(event.target.value)} placeholder="Appearance, wardrobe, and distinctive details" /></label>
          <div className="inline-character-actions">
            <button type="button" onClick={handleGenerate} disabled={busy}>
              {busyAction === "generate" ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
              {draft ? "Retry image" : "Generate image"}
            </button>
            <label className="inline-upload">
              {busyAction === "upload" ? <Loader2 size={14} className="animate-spin" /> : <Upload size={14} />}
              Upload image
              <input type="file" accept="image/*" disabled={busy} onChange={handleUpload} />
            </label>
          </div>
          {draft && (
            <div className="inline-character-draft">
              <img src={draft.image_url} alt={`${draft.name} character reference`} />
              <div>
                <span>{draft.image_source}</span>
                <label>
                  Sarvam voice
                  <select value={voiceId} onChange={(event) => setVoiceId(event.target.value)}>
                    <option value="">Choose a voice</option>
                    {SARVAM_VOICES.map((voice) => <option key={voice} value={voice}>{voice}</option>)}
                  </select>
                </label>
                <button type="button" onClick={handleSaveVoice} disabled={busy || !voiceId}>
                  {busyAction === "voice" && <Loader2 size={14} className="animate-spin" />} Save voice
                </button>
                <button type="button" className="inline-approve" onClick={handleApprove} disabled={busy || !draft.voice_id}>
                  {busyAction === "approve" ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />}
                  Approve and use
                </button>
              </div>
            </div>
          )}
          {error && <p className="resolution-error">{error}</p>}
        </section>
      )}
    </article>
  );
}

function LocationResolutionRow({ item, assets, resolution, onResolve }) {
  const [choice, setChoice] = useState("");

  function handleChoice(event) {
    const value = event.target.value;
    setChoice(value);
    if (value === "invent") {
      onResolve({ mode: "invent", name: item.displayName });
      return;
    }
    if (value.startsWith("asset:")) {
      const asset = assets.find(({ id }) => `asset:${id}` === value);
      onResolve({ mode: "asset", name: item.displayName, asset });
      return;
    }
    onResolve(null);
  }

  return (
    <article className="resolution-row" data-resolution-state={resolution ? "resolved" : "unresolved"}>
      <div className="resolution-row-heading">
        <div><span>Location</span><h3>{item.displayName}</h3></div>
        <span className={`resolution-status ${resolution ? "resolution-status--done" : ""}`}>
          {resolution ? "Resolved" : "Needs a choice"}
        </span>
      </div>
      <label>
        Resolution
        <select aria-label={`Resolve location ${item.displayName}`} value={choice} onChange={handleChoice}>
          <option value="">Choose how to resolve</option>
          {assets.map((asset) => (
            <option key={asset.id} value={`asset:${asset.id}`}>Asset: {asset.label || asset.filename}</option>
          ))}
          <option value="invent">Let the AI invent this one</option>
        </select>
      </label>
      {!assets.length && <p className="resolution-hint">No assets tagged as locations are available.</p>}
    </article>
  );
}

export default function ScriptResolutionPanel({ scriptText, extraction, initialCharacters, locationAssets, onBack, onContinue }) {
  const [characters, setCharacters] = useState(initialCharacters);
  const [resolutions, setResolutions] = useState({});
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const characterItems = useMemo(
    () => buildResolutionItems(scriptText, extraction.characters, "character"),
    [scriptText, extraction.characters],
  );
  const locationItems = useMemo(
    () => buildResolutionItems(scriptText, extraction.locations, "location"),
    [scriptText, extraction.locations],
  );
  const items = [...characterItems, ...locationItems];
  const resolvedCount = items.filter(({ id }) => Boolean(resolutions[id])).length;
  const allResolved = resolvedCount === items.length;

  function setResolution(id, resolution) {
    setSubmitError("");
    setResolutions((current) => ({ ...current, [id]: resolution }));
  }

  function addApprovedCharacter(character) {
    setCharacters((current) => [character, ...current.filter(({ id }) => id !== character.id)]);
  }

  async function handleContinue() {
    if (!allResolved || submitting) return;
    const resolutionMap = buildResolutionMap(characterItems, locationItems, resolutions);

    setSubmitting(true);
    setSubmitError("");
    try {
      await onContinue(resolutionMap);
    } catch (error) {
      setSubmitError(error.message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="script-resolution-panel" aria-label="Resolve script references">
      <button type="button" className="resolution-back" onClick={onBack}><ArrowLeft size={15} /> Back to script</button>
      <header>
        <p className="eyebrow">Script references</p>
        <h1>Resolve every named reference</h1>
        <p>Choose an existing reference, create a character here, or explicitly let the AI invent it.</p>
      </header>

      {!items.length ? (
        <div className="resolution-empty"><ImagePlus size={22} /><p>No named characters or locations were extracted.</p></div>
      ) : (
        <div className="resolution-list">
          {characterItems.map((item) => (
            <CharacterResolutionRow
              key={item.id}
              item={item}
              characters={characters}
              resolution={resolutions[item.id]}
              onResolve={(resolution) => setResolution(item.id, resolution)}
              onApproved={addApprovedCharacter}
            />
          ))}
          {locationItems.map((item) => (
            <LocationResolutionRow
              key={item.id}
              item={item}
              assets={locationAssets}
              resolution={resolutions[item.id]}
              onResolve={(resolution) => setResolution(item.id, resolution)}
            />
          ))}
        </div>
      )}

      <footer className="resolution-footer">
        <p>{resolvedCount} of {items.length} resolved</p>
        <button type="button" disabled={!allResolved || submitting} onClick={handleContinue}>
          {submitting && <Loader2 size={14} className="animate-spin" />}
          {submitting ? "Starting..." : "Continue to create shot list"}
        </button>
        {!allResolved && <span>Resolve every name before continuing.</span>}
        {submitError && <span className="resolution-error">{submitError}</span>}
      </footer>
    </section>
  );
}
