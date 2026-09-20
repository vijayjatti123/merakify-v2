import { useEffect, useRef, useState } from "react";
import { Alert, Box, Button, Checkbox, Chip, Dialog, DialogActions, DialogContent, DialogTitle,
  FormControlLabel, LinearProgress, MenuItem, Stack, TextField, Typography } from "@mui/material";
import { productAlbumRequest } from "../api/client";

const ACTIVE = ["queued", "submitting", "processing", "verifying"];
const LABELS = { queued: "Waiting", submitting: "Starting", processing: "Creating view", verifying: "Checking image",
  review: "Ready to review", approved: "Approved", rejected: "Needs a new image", failed: "Generation stopped",
  verification_unavailable: "Verification unavailable" };
const label = value => value.replaceAll("_", " ").replace(/\b\w/g, c => c.toUpperCase());

export default function ProductAlbum({ product, onClose }) {
  const [album, setAlbum] = useState(null), [selected, setSelected] = useState([]);
  const [angles, setAngles] = useState(["three_quarter", "side"]), [model, setModel] = useState("standard");
  const [originalAngle, setOriginalAngle] = useState("side"), [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [pollError, setPollError] = useState("");
  const [inspecting, setInspecting] = useState(null);
  const request = useRef(null), lock = useRef(false), epoch = useRef(0);
  useEffect(() => {
    const current = ++epoch.current;
    setAlbum(null); setSelected([]); setAck(false); setError(""); setPollError(""); setInspecting(null); request.current = null;
    productAlbumRequest(product.id).then(v => { if (epoch.current === current) setAlbum(v); })
      .catch(e => { if (epoch.current === current) setError(e.message); });
    return () => { epoch.current++; };
  }, [product.id]);
  const views = album?.views || [];
  const active = views.some(v => ACTIVE.includes(v.status));
  useEffect(() => {
    if (!active) return;
    let live = true, timer;
    const poll = async () => {
      try { const next = await productAlbumRequest(product.id); if (live) { setAlbum(next); setPollError(""); } }
      catch { if (live) setPollError("Progress could not be refreshed. Your work is saved; reconnecting…"); }
      if (live) timer = setTimeout(poll, 2500);
    };
    timer = setTimeout(poll, 2500);
    return () => { live = false; clearTimeout(timer); };
  }, [active, product.id]);
  const chosenModel = album?.models?.find(m => m.id === model);
  const estimate = Math.round(angles.length * (chosenModel?.max_generation_usd_per_view || 0) * 100) / 100;
  const selectedViews = views.filter(v => selected.includes(v.id) && v.status === "review");
  const needsAck = selectedViews.some(v => v.provenance === "inferred");
  const inspected = views.find(v => v.id === inspecting);
  async function run(action) {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError("");
    const current = epoch.current;
    try { await action(current); } catch (e) { if (current === epoch.current) setError(e.message); }
    finally { lock.current = false; if (current === epoch.current) setBusy(false); }
  }
  async function refresh(current) {
    const next = await productAlbumRequest(product.id);
    if (current === epoch.current) setAlbum(next);
  }
  async function prepare(current) {
    const fingerprint = JSON.stringify([product.id, angles, model, estimate]);
    if (request.current?.fingerprint !== fingerprint) request.current = { fingerprint, key: crypto.randomUUID() };
    await productAlbumRequest(product.id, "/prepare", { angles, model, request_key: request.current.key, max_generation_usd: estimate });
    await refresh(current);
    request.current = null;
  }
  async function approve(current) {
    await productAlbumRequest(product.id, "/approve", { view_ids: selectedViews.map(v => v.id), inferred_details_reviewed: ack });
    await refresh(current); setSelected([]); setAck(false);
  }
  async function uploadOriginal(file, current) {
    const form = new FormData(); form.append("angle", originalAngle); form.append("file", file);
    await productAlbumRequest(product.id, "/upload", form); await refresh(current);
  }
  const findings = (view, all = false) => Object.entries(view?.verdict || {}).filter(([,v]) => all || v.status !== "pass").map(([key,v]) =>
    <Typography key={key} variant="body2" color={v.status === "pass" ? "text.secondary" : "warning.main"} sx={{ mt: .5 }}>{label(key)}: {v.status} — {v.evidence}</Typography>);
  return <Box sx={{ mt: 2, p: { xs: 1.5, sm: 2 }, border: "1px solid", borderColor: "divider", borderRadius: 2 }} data-testid="product-album">
    <Stack direction="row" sx={{ justifyContent: "space-between", alignItems: "center" }}>
      <Typography variant="h6">Product album</Typography><Button onClick={onClose}>Close album</Button>
    </Stack>
    <Typography variant="body2" color="text.secondary">Review the views your commercial needs. Only approved views are available to new jobs.</Typography>
    {error && <Alert severity="error" sx={{ mt: 1 }}>{error}<Button onClick={() => run(refresh)}>Refresh album</Button></Alert>}
    {pollError && <Alert severity="warning" sx={{ mt: 1 }}>{pollError}</Alert>}
    {!album && !error && <LinearProgress aria-label="Loading album" sx={{ mt: 2 }} />}
    {album && !views.length && <Typography sx={{ my: 2 }}>No extra views yet. Upload real photos or prepare a few below.</Typography>}
    <Box sx={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(160px,1fr))", gap: 1.5, mt: 2 }}>
      {views.map(view => <Box key={view.id} data-testid={"album-view-" + view.id} sx={{ border: "1px solid", borderColor: view.status === "approved" ? "success.main" : "divider", borderRadius: 2, p: 1.25, minWidth: 0 }}>
        {view.url ? <Button aria-label={"Inspect " + label(view.angle) + " view"} onClick={() => setInspecting(view.id)} sx={{ width: "100%", p: 0 }}>
          <Box component="img" src={view.thumbnail_url || view.url} alt={label(view.angle) + " product view"} sx={{ width: "100%", aspectRatio: "1", objectFit: "contain", borderRadius: 1 }} />
        </Button> : <Box sx={{ aspectRatio: "1", display: "grid", placeItems: "center", bgcolor: "action.hover" }}>
          {ACTIVE.includes(view.status) ? <LinearProgress aria-label={LABELS[view.status]} sx={{ width: "80%" }} /> : <Typography color="text.secondary">No image available</Typography>}
        </Box>}
        <Typography fontWeight={600} sx={{ mt: 1 }}>{label(view.angle)}</Typography>
        <Chip size="small" label={view.provenance === "inferred" ? "AI-inferred view" : "Original photo"} color={view.provenance === "inferred" ? "warning" : "default"} />
        <Typography variant="body2" role="status" sx={{ mt: .5 }}>{LABELS[view.status] || view.status}</Typography>
        {view.error && <Alert severity="warning" sx={{ mt: 1 }}>{view.error}</Alert>}
        {findings(view)}
        {view.status === "review" && <FormControlLabel control={<Checkbox disabled={busy} checked={selected.includes(view.id)} onChange={() => { setAck(false); setSelected(s => s.includes(view.id) ? s.filter(id => id !== view.id) : [...s, view.id]); }} />} label="Select for approval" />}
        {view.status === "verification_unavailable" && <Button disabled={busy} size="small" onClick={() => run(async current => { await productAlbumRequest(product.id, "/" + view.id + "/verify", {}); await refresh(current); })}>Retry verification</Button>}
        {view.status === "approved" && <Button disabled={busy} size="small" onClick={() => run(async current => { await productAlbumRequest(product.id, "/" + view.id + "/withdraw", {}); await refresh(current); })}>Remove from future jobs</Button>}
      </Box>)}
    </Box>
    {selectedViews.length > 0 && <Box sx={{ mt: 2 }}>
      {needsAck && <FormControlLabel control={<Checkbox data-testid="album-inferred-ack" checked={ack} disabled={busy} onChange={e => setAck(e.target.checked)} />} label="I reviewed these AI-inferred views, including uncertain details, and confirm they accurately represent my product." />}
      <Button variant="contained" disabled={busy || (needsAck && !ack)} onClick={() => run(approve)}>Approve selected views</Button>
    </Box>}
    <Box sx={{ mt: 3, p: 1.5, bgcolor: "action.hover", borderRadius: 2 }}>
      <Typography fontWeight={600}>Prepare additional views</Typography>
      <Typography variant="body2" color="text.secondary">Choose up to three angles. AI can infer unseen surfaces incorrectly; review every view before using it.</Typography>
      <Stack direction="row" useFlexGap sx={{ mt: 1, flexWrap: "wrap" }}>
        {(album?.angles || []).map(angle => <FormControlLabel key={angle} control={<Checkbox size="small" checked={angles.includes(angle)} disabled={busy || active || (!angles.includes(angle) && angles.length >= 3)} onChange={() => setAngles(s => s.includes(angle) ? s.filter(v => v !== angle) : [...s, angle])} />} label={label(angle)} />)}
      </Stack>
      <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} sx={{ my: 1 }}>
        <TextField select size="small" label="Image model" value={album ? model : ""} disabled={busy || active || !album} onChange={e => setModel(e.target.value)} sx={{ minWidth: 190 }}>
          {(album?.models || []).map(m => <MenuItem key={m.id} value={m.id}>{m.label}</MenuItem>)}
        </TextField>
        <Button variant="contained" disabled={busy || active || !chosenModel || !angles.length} onClick={() => run(prepare)}>Prepare views · up to ~${estimate.toFixed(2)}</Button>
      </Stack>
      <Typography variant="caption" display="block">{album?.estimate_note}</Typography>
      <Typography variant="caption" display="block">Retry verification checks the saved image only. A new image requires a new preparation request after that retry.</Typography>
    </Box>
    <Box sx={{ mt: 2, p: 1.5, border: "1px dashed", borderColor: "divider", borderRadius: 2 }}>
      <Typography fontWeight={600}>Have another real product photo?</Typography>
      <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} sx={{ mt: 1 }}>
        <TextField select size="small" label="Photo angle" value={album ? originalAngle : ""} sx={{ minWidth: 190 }} disabled={busy || !album} onChange={e => setOriginalAngle(e.target.value)}>
          {(album?.angles || []).map(angle => <MenuItem key={angle} value={angle}>{label(angle)}</MenuItem>)}
        </TextField>
        <Button component="label" variant="outlined" disabled={busy || !album}>Upload original view
          <input data-testid="album-original-file" hidden type="file" accept="image/png,image/jpeg,image/webp" onChange={e => { const file = e.target.files?.[0]; e.target.value = ""; if (file) run(current => uploadOriginal(file, current)); }} />
        </Button>
      </Stack>
    </Box>
    <Dialog open={!!inspected} onClose={() => setInspecting(null)} maxWidth="lg" fullWidth>
      <DialogTitle>{inspected ? label(inspected.angle) : "Product view"} · Inspect product details</DialogTitle>
      <DialogContent><Stack direction={{ xs: "column", md: "row" }} spacing={2}>
        {product.accepted_url && <Box sx={{ flex: 1, minWidth: 0 }}><Typography>Approved master</Typography><Box component="img" src={product.accepted_url} alt="Approved product master" sx={{ width: "100%", objectFit: "contain" }} /></Box>}
        <Box sx={{ flex: 1, minWidth: 0 }}><Typography>{inspected?.provenance === "inferred" ? "AI-inferred candidate" : "Original photo"}</Typography><Box component="img" src={inspected?.url} alt="Full-resolution product view" sx={{ width: "100%", objectFit: "contain" }} /></Box>
      </Stack>{findings(inspected, true)}</DialogContent>
      <DialogActions><Button component="a" href={inspected?.url} target="_blank" rel="noopener noreferrer">Open full-size image</Button><Button onClick={() => setInspecting(null)}>Close inspection</Button></DialogActions>
    </Dialog>
  </Box>;
}
