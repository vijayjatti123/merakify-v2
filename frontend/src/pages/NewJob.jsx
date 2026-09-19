import CommercialIntake, { AD_TYPES } from "../components/CommercialIntake";
import StudioSelect from "../components/StudioSelect";
import ProductPicker from "../components/ProductPicker";
import { VIDEO_MODELS, modelNote } from "../utils/videoModels";
import { Sparkles, FileText, ChevronUp, ImagePlus, Loader2, Plus, X, Clapperboard, ArrowUpRight } from "lucide-react";
import { useState, useEffect } from "react";
import { Alert, Button, Card, TextField, Typography } from "@mui/material";
import ActionProgress from "../components/ActionProgress";
import { friendlyMessage } from "../utils/presentation";

import { extractScript, listAssets, listCharacters, uploadAsset } from "../api/client";
import ScriptResolutionPanel from "../components/ScriptResolutionPanel";

import BriefCharacterInput from "../components/BriefCharacterInput";
import ClarifierPanel from "../components/ClarifierPanel";
import { activeMentions, recordMentionJob, preserveRefinedMentions } from "../utils/characterMentions";

const COLORS = {
  bg: "var(--mui-palette-background-default)",
  panel: "var(--mui-palette-background-paper)",
  field: "var(--mui-palette-action-hover)",
  border: "var(--mui-palette-divider)",
  text: "var(--mui-palette-text-primary)",
  muted: "var(--mui-palette-text-secondary)",
  marigold: "var(--mui-palette-primary-main)",
};


const SelectField = StudioSelect;

export default function NewJob({ onSubmit, collapsed = false, submittedBrief = "", savedJob = null }) {
  const [adType, setAdType] = useState("character");
  const [commercialDrafts, setCommercialDrafts] = useState({});
  const adBrief = commercialDrafts[adType] || {};
  const commercialPayload = { ad_type: adType, ad_brief: adBrief };
  const [brief, setBrief] = useState("");
  const [characterSelections, setCharacterSelections] = useState({});
  const [scriptMode, setScriptMode] = useState(false);
  const [extraction, setExtraction] = useState(null);
  const [approvedCharacters, setApprovedCharacters] = useState([]);
  const [locationAssets, setLocationAssets] = useState([]);
  const [duration, setDuration] = useState("30 seconds");
  const [customDuration, setCustomDuration] = useState("");
  const [aspectRatio, setAspectRatio] = useState("16:9");
  const [contentType, setContentType] = useState("Ad");
  const [colorGrade, setColorGrade] = useState("None");
  const [visualStyle, setVisualStyle] = useState("Natural");
  const [modeStyles, setModeStyles] = useState({});
  function chooseCommercialType(next) {
    setModeStyles(current => ({ ...current, [adType]: visualStyle }));
    setVisualStyle(modeStyles[next] || (next === "cgi" ? "3D / CGI" : "Natural"));
    setContentType(next === "ugc" ? "UGC" : next === "product" ? "Product hero" : "Ad");
    setAdType(next);
  }
  const [quality, setQuality] = useState("720p");
  const [language, setLanguage] = useState("English");
  const [customLanguage, setCustomLanguage] = useState("");
  const [modelChoice, setModelChoice] = useState("h3_max_fal");
  const chosenModel = VIDEO_MODELS.find(model => model.id === modelChoice);
  const aiModel = chosenModel?.family || modelChoice;
  const videoModel = chosenModel?.id || null;
  useEffect(() => {
    if (savedJob) {
      setAdType(savedJob.ad_type || "character");
      setCommercialDrafts(current => ({ ...current, [savedJob.ad_type || "character"]: savedJob.ad_brief || {} }));
      setModelChoice(savedJob.video_model || savedJob.ai_model);
      setVisualStyle(savedJob.visual_style || "Natural"); setColorGrade(savedJob.color_grade || "None");
      setQuality(savedJob.quality); setLanguage(savedJob.language); setAspectRatio(savedJob.aspect_ratio);
    }
  }, [savedJob]);
  const [assetPanelOpen, setAssetPanelOpen] = useState(false);
  const [assets, setAssets] = useState([]);
  const [products, setProducts] = useState([]);
  const [selectedAsset, setSelectedAsset] = useState(null);
  const [loadingAssets, setLoadingAssets] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [assetRole, setAssetRole] = useState("");
  const [assetLabel, setAssetLabel] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [clarification, setClarification] = useState(null);

  const effectiveLanguage = language === "Other" ? customLanguage.trim() : language;
  const effectiveDuration = duration === "Custom" ? customDuration.trim() : duration;
  const knownFields = { duration: effectiveDuration, aspect_ratio: aspectRatio, content_type: contentType,
    color_grade: colorGrade, visual_style: visualStyle, quality, language: effectiveLanguage, ai_model: aiModel };
  const clarificationContext = JSON.stringify([knownFields, scriptMode, products.map(p => p.id), adType, adBrief]);
  const activeClarification = clarification?.brief === brief && clarification?.context === clarificationContext ? clarification : null;
  const clarificationPayload = activeClarification ? { clarifier_session_id: activeClarification.id, clarifier_revision: activeClarification.revision } : {};
  const modelError = videoModel === "automatic_omni_mini" && quality !== "720p"
    ? "This saved selection requires 720p. Select 720p to continue."
    : videoModel === "kling_voice_fal" && !["english", "chinese", "en", "zh", "mandarin"].includes(effectiveLanguage.toLowerCase())
    ? "Kling Voice ID supports English/Chinese only. Choose Seedance for this language." : "";
  const needsProduct = ["product", "cgi"].includes(adType) && products.length === 0;
  const canSubmit = Boolean(!needsProduct && brief.trim() && effectiveDuration && effectiveLanguage && !submitting && !modelError);
  // Guidance mirrors ClarifierPanel's existing gates; it does not control activation.
  const needsRefinementCharacters = brief.trim().length < 20;
  const needsRefinementWords = brief.trim().split(/\s+/).length < 4;
  const refinementHint = needsRefinementCharacters && needsRefinementWords
      ? "Describe your video idea (a full sentence works best) to unlock AI refinement."
      : needsRefinementWords
        ? "A few more words will unlock AI refinement."
        : needsRefinementCharacters
          ? "Add a little more detail to reach 20 characters and unlock AI refinement."
          : "";

  function handleLanguageChange(event) {
    setLanguage(event.target.value);
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
        ...clarificationPayload,
        ...commercialPayload,
        brief: briefParts.join("\n\n"),
        product_ids: products.map(p => p.id),
        character_mentions: activeMentions(brief, characterSelections),
        aspect_ratio: aspectRatio,
        visual_style: visualStyle,
        color_grade: colorGrade,
        quality,
        language: effectiveLanguage,
        ai_model: aiModel,
        video_model: videoModel,
      });
      recordMentionJob();
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
      ...clarificationPayload,
        ...commercialPayload,
      brief: briefParts.join("\n\n"),
        product_ids: products.map(p => p.id),
      aspect_ratio: aspectRatio,
      visual_style: visualStyle,
      color_grade: colorGrade,
      quality,
      language: effectiveLanguage,
      ai_model: aiModel,
      video_model: videoModel,
      script_text: brief.trim(),
      resolutions,
    });
    setExtraction(null);
  }

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
    <Card component="section" className={`intake-card ${collapsed ? "intake-card--collapsed" : ""}`} aria-label="Creative brief input" sx={{ p: 0 }}>
      <form onSubmit={handleSubmit} className="flex flex-col gap-8">
        <header className="intake-header">
          <div>
            <p className="creator-eyebrow" style={{ color: COLORS.marigold }}><Clapperboard size={16} aria-hidden="true" /> VIDEO STUDIO</p>
            <h1 className="text-3xl md:text-4xl mb-2" style={{ fontFamily: "inherit" }}>
              {collapsed ? "Brief submitted" : <>What will you <span className="hero-accent">create?</span></>}
            </h1>
            <p className="text-sm intake-summary" style={{ color: COLORS.muted }}>
              {collapsed ? submittedBrief : "Your story, directed. Start with an idea or a script."}
            </p>
            {collapsed && savedJob && <Typography variant="caption" color="text.secondary" data-testid="saved-video-model">
              {AD_TYPES.find(type => type.id === savedJob.ad_type)?.label || "Character Commercial"} · Video model: {chosenModel?.label || savedJob.ai_model}
            </Typography>}
          </div>
          {collapsed && <ChevronUp size={18} aria-hidden="true" style={{ color: COLORS.marigold }} />}
        </header>

        <div className="intake-collapse" aria-hidden={collapsed}>
          <fieldset disabled={collapsed} className="intake-collapse-inner flex flex-col gap-7">
            <CommercialIntake adType={adType} onType={chooseCommercialType} value={adBrief}
              onChange={value => setCommercialDrafts(current => ({ ...current, [adType]: value }))}
              disabled={collapsed || submitting} />
            {adType !== "character" && <ProductPicker selected={products} onChange={setProducts} disabled={collapsed || submitting} />}
            {needsProduct && <Typography variant="body2" color="text.secondary" data-testid="commercial-product-required">
              Add and approve a product photo to enable Create storyboard.
            </Typography>}
            {scriptMode ? <TextField multiline fullWidth label="Your script" value={brief}
              onChange={event => setBrief(event.target.value)} placeholder="Paste your full script or scene breakdown here"
              minRows={3} autoFocus sx={{ "& textarea": { fontSize: "1.1rem", lineHeight: 1.7 } }} /> :
              <BriefCharacterInput value={brief} onChange={setBrief} selections={characterSelections}
                onSelections={setCharacterSelections} disabled={collapsed || submitting}
                tools={adType === "character" ? <ProductPicker compact selected={products} onChange={setProducts} disabled={collapsed || submitting} /> : null} />}
            {!collapsed && !submitting && refinementHint && <Typography variant="body2" color="text.secondary"
              role="status" aria-live="polite" data-testid="clarifier-activation-hint" sx={{ mt: -1.5 }}>
              {refinementHint}
            </Typography>}
            {scriptMode && adType === "character" && <ProductPicker selected={products} onChange={setProducts} disabled={collapsed || submitting} />}
            <ClarifierPanel adType={adType} adBrief={adBrief} brief={brief} inputMode={scriptMode ? "script" : "idea"} productIds={products.map(p => p.id)}
              prepareRefined={text => scriptMode ? text : preserveRefinedMentions(text, brief, characterSelections)}
              knownFields={knownFields} disabled={collapsed || submitting} onUse={(text, row) => {
                const acceptedBrief = scriptMode ? brief : text;
                if (!scriptMode) setBrief(text);
                setClarification({ brief: acceptedBrief, context: clarificationContext, id: row.session_id, revision: row.revision, text });
              }} />
            {activeClarification && <Alert severity="success" data-testid="production-direction-saved">Your reviewed direction will guide shot planning.{scriptMode && <details><summary>View production notes (script unchanged)</summary><Typography sx={{ whiteSpace: "pre-wrap" }}>{activeClarification.text}</Typography></details>}</Alert>}
            {clarification && !activeClarification && <Alert severity="info">Your inputs changed. Refine again to update your production direction, or continue with the current inputs.</Alert>}
            <Button
              type="button"
              className="script-mode-toggle" startIcon={<FileText size={18} />}
              aria-pressed={scriptMode}
              onClick={() => {
                setScriptMode((current) => !current);
                setExtraction(null);
                setError("");
              }}
            >
              {scriptMode ? "Use an idea instead" : "Paste a script instead"}
            </Button>

            <div className="intake-options">
              <SelectField label="Format" value={aspectRatio} onChange={event => setAspectRatio(event.target.value)}>
                <option value="16:9">16:9 Landscape</option><option value="9:16">9:16 Portrait</option>
              </SelectField>
              <SelectField label="Duration" value={duration} onChange={event => setDuration(event.target.value)}>
                <option>15 seconds</option><option>30 seconds</option><option>60 seconds</option><option>Custom</option>
              </SelectField>
              <SelectField label="Language" value={language} onChange={handleLanguageChange}>
                <option>English</option><option>Hindi</option><option>Tamil</option><option>Telugu</option><option>Bengali</option><option>Other</option>
              </SelectField>
            </div>

            <details data-testid="advanced-video-settings"><summary style={{ cursor: "pointer" }}>Style, quality & model</summary>
              <div className="intake-options" style={{ marginTop: 16 }}>
              <SelectField label="Color grade" value={colorGrade} onChange={(event) => setColorGrade(event.target.value)}>
                {["None", "Warm", "Cool", "Vintage", "Neon", "Black & white", "Vibrant"].map((value) => <option key={value}>{value}</option>)}
              </SelectField>
              <SelectField label="Visual Style" value={visualStyle} onChange={(event) => setVisualStyle(event.target.value)}>
                {["Natural", "Cinematic", "Realistic", "Cartoon / Anime", "3D / CGI", "Hyper-realistic", "Vintage / retro film"].map((value) => <option key={value}>{value}</option>)}
              </SelectField>
              <SelectField label="Video quality" value={quality} onChange={(event) => setQuality(event.target.value)}>
                <option>480p</option><option>720p</option>
              </SelectField>
              <SelectField label="AI model" value={modelChoice} onChange={(event) => setModelChoice(event.target.value)} note={modelError || (chosenModel ? modelNote(videoModel) : "Existing planning model; video generation support may be limited.")}>
                <optgroup label="Video generation">
                  {VIDEO_MODELS.filter(model => model.id !== "automatic_omni_mini" || modelChoice === model.id).map(model => <option key={model.id} value={model.id}>{model.label}</option>)}
                </optgroup>
                <optgroup label="Other planning models">
                  {["Wan 2.5", "Seedance 2.5", "Veo 3.1", "Sora 2"].map(model => <option key={model} value={model}>{model}</option>)}
                </optgroup>
                {collapsed && ["Seedance 2.0", "Kling 3.0"].includes(modelChoice) && <option value={modelChoice}>{modelChoice}</option>}
              </SelectField>
              <Button type="button" onClick={toggleAssetPanel} aria-label="Add reference image" variant="outlined" color="secondary" className="reference-toggle">
                {assetPanelOpen ? <X size={18} /> : <ImagePlus size={18} />} Reference image
              </Button>
              </div>
            </details>

            {modelError && <Alert severity="warning">{modelError}</Alert>}
            {duration === "Custom" && <input value={customDuration} onChange={(event) => setCustomDuration(event.target.value)} aria-label="Custom duration" placeholder="Custom duration, e.g. 45 seconds" className="w-full max-w-xs rounded-md px-3 py-2 text-sm outline-none" style={{ background: COLORS.field, border: `1px solid ${COLORS.border}`, color: COLORS.text }} />}
            {language === "Other" && <input value={customLanguage} onChange={(event) => setCustomLanguage(event.target.value)} aria-label="Custom language" placeholder="Enter a language" className="w-full max-w-xs rounded-md px-3 py-2 text-sm outline-none" style={{ background: COLORS.field, border: `1px solid ${COLORS.border}`, color: COLORS.text }} />}

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
                  <ActionProgress label="Loading your reference images…" />
                ) : assets.length ? (
                  <div className="grid grid-cols-2 sm:grid-cols-4 md:grid-cols-6 gap-3">
                    {assets.map((asset) => {
                      const selected = selectedAsset?.id === asset.id;
                      return (
                        <Button type="button" key={asset.id} onClick={() => setSelectedAsset(asset)} className="rounded-md overflow-hidden text-left" style={{ border: `2px solid ${selected ? COLORS.marigold : COLORS.border}`, background: COLORS.field }}>
                          <img src={asset.url} alt="" className="w-full h-20 object-cover" />
                          <span className="block text-[10px] px-2 py-1 truncate" style={{ color: COLORS.muted }}>{asset.filename}</span>
                        </Button>
                      );
                    })}
                  </div>
                ) : <p className="text-xs" style={{ color: COLORS.muted }}>No reference images yet.</p>}
              </section>
            )}

            {selectedAsset && (
              <div className="flex items-center gap-2 text-xs" style={{ color: COLORS.muted }}>
                <span>Reference: {selectedAsset.filename}</span>
                <Button type="button" onClick={() => setSelectedAsset(null)} style={{ color: COLORS.marigold }}>Remove</Button>
              </div>
            )}
            {error && <Alert severity="error">{friendlyMessage(error, "We couldn't start your video. Please try again.")}</Alert>}
            {submitting && <ActionProgress label={scriptMode ? "Reading your script…" : "Starting your video plan…"} />}
            {uploading && <ActionProgress label="Uploading your reference image…" />}
            <Button type="submit" disabled={!canSubmit} variant="contained" className="studio-create-button" endIcon={!submitting ? <ArrowUpRight size={17} /> : undefined}>
              {submitting && <Loader2 size={15} className="animate-spin" />}
              {submitting ? (scriptMode ? "Reading your script…" : "Starting...") : (scriptMode ? "Choose characters and places" : "Create storyboard")}
            </Button>
          </fieldset>
        </div>
      </form>
    </Card>
  );
}
