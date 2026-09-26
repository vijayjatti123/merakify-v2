import { useEffect, useState } from "react";
import { Alert, Box, Button, Checkbox, Dialog, DialogActions, DialogContent, DialogTitle, FormControlLabel, LinearProgress, MenuItem, Stack, TextField, Typography } from "@mui/material";
import { RefreshCw, Upload } from "lucide-react";
import { replaceShotPreview } from "../api/client";

export default function ShotImageActions({ jobId, shot, aspectRatio, disabled, onRefresh, repairMarkings = false }) {
  const [mode, setMode] = useState(null);
  const [hint, setHint] = useState("");
  const [file, setFile] = useState(null);
  const [fileUrl, setFileUrl] = useState("");
  const [fit, setFit] = useState("crop");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [acknowledge, setAcknowledge] = useState(false);
  const candidate = shot.preview_replacement;
  const pending = candidate?.status === "working";
  const ready = candidate?.status === "ready";
  const ratio = (aspectRatio || "16:9").replace(":", "/");
  const markingHint = "Correct the product's printed brand name to match the approved shot plan exactly. Remove misspelled, duplicated, partial, or invented letters. Keep the same product shape, camera framing, lighting, background, and physical placement. Do not add other text.";
  useEffect(() => {
    if (!file) { setFileUrl(""); return; }
    const url = URL.createObjectURL(file); setFileUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);
  useEffect(() => { setAcknowledge(false); }, [candidate?.token]);
  async function act(action, payload) {
    setBusy(true); setError("");
    try {
      await replaceShotPreview(jobId, shot.shot_number, action, payload);
      setMode(null); setFile(null); setHint("");
      await onRefresh();
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }
  function submit() {
    if (mode === "upload") {
      const data = new FormData(); data.append("file", file);
      data.append("expected_key", shot.still_frame_key || ""); data.append("fit", fit);
      return act("upload", data);
    }
    return act("replacement", { expected_key: shot.still_frame_key || "", hint });
  }
  return <Box sx={{ my: 2 }} data-testid={`image-actions-${shot.shot_number}`}>
    {repairMarkings && !pending && !ready && <Alert severity="warning" sx={{ mb: 1 }}>
      The product lettering did not hold up in the video. We can make a corrected image for this shot while keeping the others and the saved voice.
      <Stack direction="row" useFlexGap flexWrap="wrap" spacing={1} sx={{ mt: 1 }}>
        <Button variant="contained" disabled={disabled || busy} onClick={() => act("replacement", { expected_key: shot.still_frame_key || "", hint: markingHint })} data-testid={`fix-product-image-${shot.shot_number}`}>Fix product image for me</Button>
        <Button disabled={disabled || busy} onClick={() => { setError(""); setMode("upload"); }}>Upload a product image</Button>
      </Stack>
    </Alert>}
    {shot.video_source_changed && shot.video_status === "review_required" && <Alert severity="info" sx={{ mb: 1 }}>Your new image is ready. Use “Regenerate corrected video” below to make this shot with the new image.</Alert>}
    <Stack direction="row" useFlexGap flexWrap="wrap" spacing={1}>
      <Button startIcon={<RefreshCw size={16} />} variant="outlined" disabled={disabled || busy || pending || ready} onClick={() => { setError(""); setMode("generate"); }} data-testid={`regenerate-image-${shot.shot_number}`}>Regenerate image</Button>
      <Button startIcon={<Upload size={16} />} disabled={disabled || busy || pending || ready} onClick={() => { setError(""); setMode("upload"); }} data-testid={`upload-image-${shot.shot_number}`}>Upload image</Button>
    </Stack>
    {error && !mode && <Alert severity="error" sx={{ mt: 1 }}>{error}</Alert>}
    {pending && <Box role="status" sx={{ mt: 2 }}><Typography variant="body2">Creating and checking your replacement. Your current image stays available.</Typography><LinearProgress sx={{ mt: 1 }} /><Button disabled={busy} onClick={() => act("decision", { token: candidate.token, accept: false })}>Keep current image</Button></Box>}
    {candidate?.status === "failed" && <Alert severity="warning" sx={{ mt: 2 }}>{candidate.warning}</Alert>}
    {ready && <Box sx={{ mt: 2, p: 2, borderRadius: 3, bgcolor: "action.hover" }} data-testid={`image-comparison-${shot.shot_number}`}>
      <Typography variant="subtitle1">Choose your shot image</Typography>
      <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", sm: "1fr 1fr" }, gap: 2, my: 1 }}>
        {[{ label: "Current", url: shot.still_frame_url }, { label: "Replacement", url: candidate.url }].map(({ label, url }) => <Box key={label}><Typography variant="caption">{label}</Typography>{url ? <Box component="img" src={url} alt={`${label} image for shot ${shot.shot_number}`} sx={{ width: "100%", aspectRatio: ratio, objectFit: "contain", borderRadius: 2 }} /> : <Typography variant="body2">Image unavailable</Typography>}</Box>)}
      </Box>
      {candidate.warning && <><Alert severity="warning">{candidate.warning.startsWith("This image differs") ? <><Typography variant="body2">This image may not match your planned scene. Please review it before continuing.</Typography><Box component="details" sx={{ mt: 1 }}><summary>Why this image was flagged</summary><Typography variant="body2">{candidate.warning}</Typography></Box></> : candidate.warning}</Alert><FormControlLabel control={<Checkbox checked={acknowledge} onChange={e => setAcknowledge(e.target.checked)} />} label="I've reviewed this difference and want to use this image." /></>}
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>Only this shot changes. Speech stays saved. An existing video will need regeneration before you combine the final video.</Typography>
      <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap">
        <Button variant="contained" data-testid={`accept-image-${shot.shot_number}`} disabled={disabled || busy || !candidate.url || Boolean(candidate.warning && !acknowledge)} onClick={() => act("decision", { token: candidate.token, accept: true, acknowledge })}>Use replacement</Button>
        <Button data-testid={`keep-image-${shot.shot_number}`} disabled={busy} onClick={() => act("decision", { token: candidate.token, accept: false })}>Keep current</Button>
      </Stack>
    </Box>}
    <Dialog open={Boolean(mode)} onClose={() => !busy && setMode(null)} fullWidth maxWidth="sm">
      <DialogTitle>{mode === "upload" ? "Upload a shot image" : "Regenerate this shot image"} · Shot {shot.shot_number}</DialogTitle>
      <DialogContent>
        <Typography sx={{ mb: 2 }}>Your current image stays until you review and accept a replacement. This does not generate a video.</Typography>
        {mode === "upload" ? <Stack spacing={2}>
          <Button component="label" variant="outlined">Choose image<input data-testid="replacement-file" type="file" accept="image/png,image/jpeg,image/webp" hidden onChange={e => { setFile(e.target.files?.[0] || null); setError(""); }} /></Button>
          <Typography variant="caption">PNG, JPEG or WebP · up to 15 MB · at least 256 pixels on each side</Typography>
          {file && <Typography variant="body2">{file.name}</Typography>}
          <TextField select label="Image framing" value={fit} onChange={e => setFit(e.target.value)}><MenuItem value="crop">Crop to fill (centered)</MenuItem><MenuItem value="fit">Fit whole image (add borders)</MenuItem></TextField>
          {fileUrl && <Box sx={{ aspectRatio: ratio, bgcolor: "#f4f1ed", overflow: "hidden", borderRadius: 2 }}><Box component="img" src={fileUrl} alt="Upload framing preview" sx={{ width: "100%", height: "100%", objectFit: fit === "crop" ? "cover" : "contain" }} /></Box>}
        </Stack> : <><TextField autoFocus fullWidth multiline minRows={2} label="What would you like to change? (optional)" value={hint} inputProps={{ maxLength: 1000 }} onChange={e => setHint(e.target.value)} /><Typography variant="caption">Uses your existing shot plan and references. Image-generation charges apply.</Typography></>}
        {error && <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert>}
        {busy && <LinearProgress sx={{ mt: 2 }} />}
      </DialogContent>
      <DialogActions><Button disabled={busy} onClick={() => setMode(null)}>Cancel</Button><Button data-testid="submit-image-replacement" variant="contained" disabled={busy || (mode === "upload" && !file)} onClick={submit}>{mode === "upload" ? "Upload and check" : "Create replacement"}</Button></DialogActions>
    </Dialog>
  </Box>;
}
