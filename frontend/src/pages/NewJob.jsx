import { ChevronUp, ImagePlus, Loader2, Plus, X } from "lucide-react";
import { useState } from "react";

import { extractScript, listAssets, listCharacters, uploadAsset } from "../api/client";
import ScriptResolutionPanel from "../components/ScriptResolutionPanel";

const COLORS = {
  bg: "#13141F",
  panel: "#1B1D2B",
  field: "#0F1019",
  border: "#2E3145",
  text: "#F3F0E8",
  muted: "#9694A8",
  marigold: "#E8A33D",
};

const MODEL_TIERS = [
  { label: "Decent", models: ["Wan 2.5", "Seedance 2.0"] },
  { label: "Better", models: ["Kling 3.0", "Seedance 2.5"] },
  { label: "Best", models: ["Veo 3.1", "Sora 2"] },
];
const NON_ENGLISH_MODELS = new Set(["Seedance 2.0", "Seedance 2.5"]);

function SelectField({ label, note, value, onChange, children }) {
  return (
    <label className="flex flex-col gap-1 min-w-[132px]">
      <span className="text-xs" style={{ color: COLORS.muted }}>{label}</span>
      <select
        value={value}
        onChange={onChange}
        className="rounded-md px-3 py-2 text-sm outline-none"
        style={{ background: COLORS.field, border: `1px solid ${COLORS.border}`, color: COLORS.text }}
      >
        {children}
      </select>
      {note && <span className="text-[10px] leading-tight max-w-[150px]" style={{ color: COLORS.muted }}>{note}</span>}
    </label>
  );
}

export default function NewJob({ onSubmit, collapsed = false, submittedBrief = "" }) {
  const [brief, setBrief] = useState("");
  const [scriptMode, setScriptMode] = useState(false);
  const [extraction, setExtraction] = useState(null);
  const [approvedCharacters, setApprovedCharacters] = useState([]);
  const [locationAssets, setLocationAssets] = useState([]);
  const [duration, setDuration] = useState("30 seconds");
  const [customDuration, setCustomDuration] = useState("");
  const [aspectRatio, setAspectRatio] = useState("16:9");
  const [contentType, setContentType] = useState("Ad");
  const [quality, setQuality] = useState("720p");
  const [language, setLanguage] = useState("English");
  const [customLanguage, setCustomLanguage] = useState("");
  const [aiModel, setAiModel] = useState("Seedance 2.5");
  const [assetPanelOpen, setAssetPanelOpen] = useState(false);
  const [assets, setAssets] = useState([]);
  const [selectedAsset, setSelectedAsset] = useState(null);
  const [loadingAssets, setLoadingAssets] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [assetRole, setAssetRole] = useState("");
  const [assetLabel, setAssetLabel] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  const effectiveLanguage = language === "Other" ? customLanguage.trim() : language;
  const effectiveDuration = duration === "Custom" ? customDuration.trim() : duration;
  const canSubmit = Boolean(brief.trim() && effectiveDuration && effectiveLanguage && !submitting);

  function handleLanguageChange(event) {
    const nextLanguage = event.target.value;
    setLanguage(nextLanguage);
    if (nextLanguage !== "English" && !NON_ENGLISH_MODELS.has(aiModel)) setAiModel("Seedance 2.5");
  }

  async function toggleAssetPanel() {
    const opening = !assetPanelOpen;
    setAssetPanelOpen(opening);
    if (!opening || assets.length) return;
    setLoadingAssets(true);
    setError("");
    try {
      setAssets(await listAssets());
    } catch (loadError) {
      setError(loadError.message);
    } finally {
      setLoadingAssets(false);
    }
  }

  async function handleUpload(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    setUploading(true);
    setError("");
    try {
      const asset = await uploadAsset(file, { role: assetRole, label: assetLabel });
      setAssets((current) => [asset, ...current.filter((item) => item.id !== asset.id)]);
      setSelectedAsset(asset);
    } catch (uploadError) {
      setError(uploadError.message);
    } finally {
      setUploading(false);
      event.target.value = "";
    }
  }

  async function handleSubmit(event) {
    event.preventDefault();
    if (!canSubmit) return;
    if (scriptMode) {
      setSubmitting(true);
      setError("");
      try {
        const [extracted, characters, availableAssets] = await Promise.all([
          extractScript(brief.trim()),
          listCharacters(),
          listAssets(),
        ]);
        setExtraction(extracted);
        setApprovedCharacters(characters);
        setLocationAssets(availableAssets.filter((asset) => asset.role === "location"));
      } catch (extractError) {
        setError(extractError.message);
      } finally {
        setSubmitting(false);
      }
      return;
    }
    const briefParts = [brief.trim(), `Target duration: ${effectiveDuration}. Content type: ${contentType}.`];
    if (selectedAsset) briefParts.push(`Reference image: ${selectedAsset.url}`);

    setSubmitting(true);
    setError("");
    try {
      await onSubmit({
        brief: briefParts.join("\n\n"),
        aspect_ratio: aspectRatio,
        quality,
        language: effectiveLanguage,
        ai_model: aiModel,
      });
    } catch (submitError) {
      setError(submitError.message);
    } finally {
      setSubmitting(false);
    }
  }

  async function handleResolvedScriptSubmit(resolutions) {
    const briefParts = [brief.trim(), `Target duration: ${effectiveDuration}. Content type: ${contentType}.`];
    if (selectedAsset) briefParts.push(`Reference image: ${selectedAsset.url}`);
    await onSubmit({
      brief: briefParts.join("\n\n"),
      aspect_ratio: aspectRatio,
      quality,
      language: effectiveLanguage,
      ai_model: aiModel,
      script_text: brief.trim(),
      resolutions,
    });
    setExtraction(null);
  }

  const visibleTiers = MODEL_TIERS.map((tier) => ({
    ...tier,
    models: language === "English" ? tier.models : tier.models.filter((model) => NON_ENGLISH_MODELS.has(model)),
  })).filter((tier) => tier.models.length);

  if (scriptMode && extraction) {
    return (
      <ScriptResolutionPanel
        scriptText={brief}
        extraction={extraction}
        initialCharacters={approvedCharacters}
        locationAssets={locationAssets}
        onBack={() => setExtraction(null)}
        onContinue={handleResolvedScriptSubmit}
      />
    );
  }

  return (
    <section className={`intake-card ${collapsed ? "intake-card--collapsed" : ""}`} aria-label="Creative brief input">
      <form onSubmit={handleSubmit} className="flex flex-col gap-5">
        <header className="intake-header">
          <div>
            <p className="text-xs uppercase tracking-[0.2em] mb-2" style={{ color: COLORS.marigold }}>Merakify Director</p>
            <h1 className="text-3xl md:text-4xl mb-2" style={{ fontFamily: "'Fraunces', serif" }}>
              {collapsed ? "Brief submitted" : "What should we make?"}
            </h1>
            <p className="text-sm intake-summary" style={{ color: COLORS.muted }}>
              {collapsed ? submittedBrief : "One sentence is enough. The defaults below are ready to go."}
            </p>
          </div>
          {collapsed && <ChevronUp size={18} aria-hidden="true" style={{ color: COLORS.marigold }} />}
        </header>

        <div className="intake-collapse" aria-hidden={collapsed}>
          <fieldset disabled={collapsed} className="intake-collapse-inner flex flex-col gap-5">
            <textarea
              value={brief}
              onChange={(event) => setBrief(event.target.value)}
              placeholder={scriptMode ? "Paste your full script or scene breakdown here" : "A joyful jewellery ad about a daughter surprising her mother"}
              rows={7}
              autoFocus
              className="w-full rounded-xl p-5 text-lg outline-none resize-none"
              style={{ background: COLORS.panel, border: `1px solid ${COLORS.border}`, color: COLORS.text }}
            />
            <button
              type="button"
              className="script-mode-toggle"
              aria-pressed={scriptMode}
              onClick={() => {
                setScriptMode((current) => !current);
                setExtraction(null);
                setError("");
              }}
            >
              {scriptMode ? "Use an idea instead" : "Paste a script instead"}
            </button>

            <div className="flex flex-wrap items-start gap-3">
              <SelectField label="Duration" value={duration} onChange={(event) => setDuration(event.target.value)}>
                <option value="15 seconds">15s</option><option value="30 seconds">30s</option>
                <option value="60 seconds">60s</option><option value="Custom">Custom</option>
              </SelectField>
              <SelectField label="Aspect ratio" value={aspectRatio} onChange={(event) => setAspectRatio(event.target.value)}>
                <option value="9:16">Portrait (9:16)</option><option value="16:9">Landscape (16:9)</option>
              </SelectField>
              <SelectField label="Content type" value={contentType} onChange={(event) => setContentType(event.target.value)}>
                <option>Ad</option><option>Short story</option><option>Documentary</option><option>Other</option>
              </SelectField>
              <SelectField label="Quality" note="Applies once video rendering is live" value={quality} onChange={(event) => setQuality(event.target.value)}>
                <option>480p</option><option>720p</option>
              </SelectField>
              <SelectField label="Language" value={language} onChange={handleLanguageChange}>
                <option>English</option><option>Hindi</option><option>Tamil</option><option>Telugu</option><option>Bengali</option><option>Other</option>
              </SelectField>
              <SelectField label="AI model" value={aiModel} onChange={(event) => setAiModel(event.target.value)}>
                {visibleTiers.map((tier) => (
                  <optgroup key={tier.label} label={tier.label}>
                    {tier.models.map((model) => <option key={model}>{model}</option>)}
                  </optgroup>
                ))}
              </SelectField>
              <button type="button" onClick={toggleAssetPanel} aria-label="Add reference asset" className="mt-5 h-9 w-9 rounded-md flex items-center justify-center" style={{ background: assetPanelOpen ? COLORS.marigold : COLORS.panel, border: `1px solid ${assetPanelOpen ? COLORS.marigold : COLORS.border}`, color: assetPanelOpen ? COLORS.bg : COLORS.text }}>
                {assetPanelOpen ? <X size={17} /> : <Plus size={17} />}
              </button>
            </div>

            {duration === "Custom" && <input value={customDuration} onChange={(event) => setCustomDuration(event.target.value)} placeholder="Custom duration, e.g. 45 seconds" className="w-full max-w-xs rounded-md px-3 py-2 text-sm outline-none" style={{ background: COLORS.field, border: `1px solid ${COLORS.border}`, color: COLORS.text }} />}
            {language === "Other" && <input value={customLanguage} onChange={(event) => setCustomLanguage(event.target.value)} placeholder="Enter a language" className="w-full max-w-xs rounded-md px-3 py-2 text-sm outline-none" style={{ background: COLORS.field, border: `1px solid ${COLORS.border}`, color: COLORS.text }} />}

            {assetPanelOpen && (
              <section className="rounded-xl p-4 flex flex-col gap-4" style={{ background: COLORS.panel, border: `1px solid ${COLORS.border}` }}>
                <div className="flex flex-wrap gap-3">
                  <label className="flex flex-col gap-1 min-w-[160px]">
                    <span className="text-xs" style={{ color: COLORS.muted }}>Role (optional)</span>
                    <select value={assetRole} onChange={(event) => setAssetRole(event.target.value)} className="rounded-md px-3 py-2 text-sm outline-none" style={{ background: COLORS.field, border: `1px solid ${COLORS.border}`, color: COLORS.text }}>
                      <option value="">No role</option>
                      <option value="background">Background</option>
                      <option value="location">Location</option>
                      <option value="prop">Prop</option>
                      <option value="product">Product</option>
                      <option value="other">Other</option>
                    </select>
                  </label>
                  <label className="flex flex-col gap-1 flex-1 min-w-[220px]">
                    <span className="text-xs" style={{ color: COLORS.muted }}>Label (optional)</span>
                    <input value={assetLabel} onChange={(event) => setAssetLabel(event.target.value)} maxLength={120} placeholder="e.g. the shop's storefront" className="rounded-md px-3 py-2 text-sm outline-none" style={{ background: COLORS.field, border: `1px solid ${COLORS.border}`, color: COLORS.text }} />
                  </label>
                </div>
                <label className="rounded-md px-4 py-3 flex items-center justify-center gap-2 text-sm cursor-pointer" style={{ border: `1px dashed ${COLORS.marigold}`, color: COLORS.marigold }}>
                  {uploading ? <Loader2 size={16} className="animate-spin" /> : <ImagePlus size={16} />}
                  {uploading ? "Uploading..." : "Upload from your device"}
                  <input type="file" accept="image/*" disabled={uploading} onChange={handleUpload} className="hidden" />
                </label>
                {loadingAssets ? (
                  <p className="text-sm flex items-center gap-2" style={{ color: COLORS.muted }}><Loader2 size={14} className="animate-spin" /> Loading library...</p>
                ) : assets.length ? (
                  <div className="grid grid-cols-2 sm:grid-cols-4 md:grid-cols-6 gap-3">
                    {assets.map((asset) => {
                      const selected = selectedAsset?.id === asset.id;
                      return (
                        <button type="button" key={asset.id} onClick={() => setSelectedAsset(asset)} className="rounded-md overflow-hidden text-left" style={{ border: `2px solid ${selected ? COLORS.marigold : COLORS.border}`, background: COLORS.field }}>
                          <img src={asset.url} alt="" className="w-full h-20 object-cover" />
                          <span className="block text-[10px] px-2 py-1 truncate" style={{ color: COLORS.muted }}>{asset.filename}</span>
                        </button>
                      );
                    })}
                  </div>
                ) : <p className="text-xs" style={{ color: COLORS.muted }}>No uploaded assets yet.</p>}
              </section>
            )}

            {selectedAsset && (
              <div className="flex items-center gap-2 text-xs" style={{ color: COLORS.muted }}>
                <span>Reference: {selectedAsset.filename}</span>
                <button type="button" onClick={() => setSelectedAsset(null)} style={{ color: COLORS.marigold }}>Remove</button>
              </div>
            )}
            {error && <p className="text-sm" style={{ color: "#C1453B" }}>{error}</p>}
            <button type="submit" disabled={!canSubmit} className="self-start px-6 py-3 rounded-md text-sm font-semibold flex items-center gap-2" style={{ background: canSubmit ? COLORS.marigold : COLORS.border, color: canSubmit ? COLORS.bg : COLORS.muted, cursor: canSubmit ? "pointer" : "not-allowed" }}>
              {submitting && <Loader2 size={15} className="animate-spin" />}
              {submitting ? (scriptMode ? "Extracting..." : "Starting...") : (scriptMode ? "Continue to resolve names" : "Create shot list")}
            </button>
          </fieldset>
        </div>
      </form>
    </section>
  );
}
