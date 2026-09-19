import { useEffect, useRef, useState } from "react";
import { Alert, Box, Button, Card, Chip, Dialog, DialogActions, DialogContent, DialogTitle,
  LinearProgress, Slider, Stack, TextField, Typography } from "@mui/material";
import { Package, Plus } from "lucide-react";
import { productRequest } from "../api/client";

export default function ProductPicker({ selected, onChange, disabled, compact = false }) {
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState([]);
  const [file, setFile] = useState(null);
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  const [horizontal, setHorizontal] = useState([0, 100]);
  const [vertical, setVertical] = useState([0, 100]);
  const [active, setActive] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    if (!file) { setUrl(""); return; }
    const value = URL.createObjectURL(file); setUrl(value);
    return () => URL.revokeObjectURL(value);
  }, [file]);
  useEffect(() => {
    if (!open) return;
    let live = true;
    productRequest().then(data => { if (live) setRows(data); }).catch(() => { if(live) setError("Couldn't load your products. Try opening this again."); });
    return () => { live = false; };
  }, [open]);
  useEffect(() => {
    if (!open || !active || !["processing", "submitting"].includes(active.status)) return;
    let live = true;
    const timer = setTimeout(async () => {
      try { const next = await productRequest(`/${active.id}`); if (live) setActive(next); }
      catch { if(live) { setError("Couldn't check progress. Your uploaded photo is saved; reopen it to resume."); setActive(null); } }
    }, 2000);
    return () => { live = false; clearTimeout(timer); };
  }, [open, active]);
  async function run(action) {
    if (busy) return;
    setBusy(true); setError("");
    try { await action(); }
    catch (e) { if (mounted.current) setError(e.message); }
    finally { if (mounted.current) setBusy(false); }
  }
  async function upload() {
    const form = new FormData(); form.append("file", file); form.append("name", name.trim());
    form.append("crop", JSON.stringify([horizontal[0]/100, vertical[0]/100,
      (horizontal[1]-horizontal[0])/100, (vertical[1]-vertical[0])/100]));
    const row = await productRequest("", form); setActive(row); setRows(old => [row, ...old]); setFile(null);
  }
  async function choose(version) {
    const row = await productRequest(`/${active.id}/approve`, { version });
    onChange([...selected.filter(p => p.id !== row.id), row]); setActive(null); setOpen(false);
  }
  const working = active && ["processing", "submitting"].includes(active.status);
  return <Box data-testid="product-picker">
    <Stack direction="row" spacing={1} sx={{ flexWrap: "wrap" }} useFlexGap>
      <Button type="button" variant="outlined" startIcon={<Package size={18}/>} disabled={disabled || selected.length >= 4}
        onClick={() => { setError(""); setOpen(true); }} data-testid="add-product">Add product</Button>
      {selected.map(p => <Chip key={p.id} label={p.name} avatar={<img src={p.accepted_url} alt=""/>}
        disabled={disabled} onDelete={() => onChange(selected.filter(v => v.id !== p.id))} />)}
    </Stack>
    {!compact && <Typography variant="caption" color="text.secondary">Add a product photo to keep its appearance consistent. No voice or description needed.</Typography>}
    <Dialog open={open} onClose={() => !busy && setOpen(false)} fullWidth maxWidth="md">
      <DialogTitle>Your product reference</DialogTitle>
      <DialogContent><Stack spacing={2} sx={{ pt: 1 }}>
        {error && <Alert severity="error">{error}</Alert>}
        {busy && <LinearProgress aria-label="Saving product"/>}
        {!active && <>
          <Button component="label" variant="contained" startIcon={<Plus size={18}/>} disabled={busy}>Upload product photo
            <input data-testid="product-file" hidden type="file" accept="image/png,image/jpeg,image/webp" onChange={e => {
              const next = e.target.files?.[0]; if(next) { setFile(next); setHorizontal([0,100]); setVertical([0,100]); }
              e.target.value = "";
            }}/>
          </Button>
          {file && <>
            <TextField label="Product name" value={name} onChange={e => setName(e.target.value)} inputProps={{ maxLength:120 }} data-testid="product-name"/>
            <Typography>Select just the product. Leave out bowls, props and other items; keep the whole package and label inside the box.</Typography>
            <Box sx={{ position:"relative", maxWidth:480, alignSelf:"center", width:"100%", overflow:"hidden" }}>
              <Box component="img" src={url} alt="Original product photo" sx={{ width:"100%", display:"block" }}/>
              <Box data-testid="product-crop-overlay" sx={{ position:"absolute", left:`${horizontal[0]}%`, top:`${vertical[0]}%`,
                width:`${horizontal[1]-horizontal[0]}%`, height:`${vertical[1]-vertical[0]}%`, border:"2px solid", borderColor:"primary.main",
                boxShadow:"0 0 0 1000px #0007", pointerEvents:"none", boxSizing:"border-box" }}/>
            </Box>
            <Typography id="product-horizontal">Left and right edges</Typography>
            <Slider value={horizontal} disableSwap aria-labelledby="product-horizontal" getAriaLabel={i => i ? "Right crop edge" : "Left crop edge"}
              valueLabelDisplay="auto" onChange={(_,v) => { if(v[1]-v[0]>=10) setHorizontal(v); }}/>
            <Typography id="product-vertical">Top and bottom edges</Typography>
            <Slider value={vertical} disableSwap aria-labelledby="product-vertical" getAriaLabel={i => i ? "Bottom crop edge" : "Top crop edge"}
              valueLabelDisplay="auto" onChange={(_,v) => { if(v[1]-v[0]>=10) setVertical(v); }}/>
            <Button variant="contained" disabled={busy || !name.trim()} onClick={() => run(upload)} data-testid="save-product-crop">Save selected area</Button>
          </>}
          {!file && rows.map(p => <Card key={p.id} variant="outlined" sx={{ p:2 }}>
            <Stack direction="row" spacing={2} alignItems="center">
              <Box component="img" src={p.accepted_url || p.crop_url} alt={p.name} sx={{ width:64, height:64, objectFit:"contain" }}/>
              <Box sx={{ flex:1 }}><Typography>{p.name}</Typography><Typography variant="caption">{p.status === "approved" ? "Approved reference" : "Needs review"}</Typography></Box>
              <Button disabled={busy} onClick={() => p.status === "approved" ? (onChange([...selected.filter(v => v.id !== p.id), p]), setOpen(false)) : setActive(p)}>
                {p.status === "approved" ? "Use product" : "Continue"}</Button>
            </Stack>
          </Card>)}
        </>}
        {active && <>
          <Typography variant="h6">{active.name}</Typography>
          {working && <><Typography role="status">Preparing your product photo… You can close this and return later.</Typography><LinearProgress/></>}
          {active.error && <Alert severity="warning">{active.error}</Alert>}
          <Stack direction={{xs:"column",sm:"row"}} spacing={2}>
            {[{url:active.crop_url,label:"Your selected original"},{url:active.prepared_url,label:"Prepared reference"}].filter(p => p.url).map(p =>
              <Box key={p.label} sx={{ flex:1, minWidth:0 }}><Typography>{p.label}</Typography><Box component="img" src={p.url} alt={p.label} sx={{ width:"100%", maxHeight:380, objectFit:"contain" }}/></Box>)}
          </Stack>
          <Alert severity="info">Check the logo, lettering, shape and edges. Preparation removes backgrounds; it may retain unwanted objects. Only approve an image that accurately represents your product.</Alert>
          {active.status === "uploaded" && <Button variant="contained" disabled={busy} onClick={() => run(async () => setActive(await productRequest(`/${active.id}/prepare`, {})))} data-testid="prepare-product">
            Prepare clean background · approximately $0.024</Button>}
          {!working && <Stack direction={{xs:"column",sm:"row"}} spacing={1}>
            {active.prepared_url && <Button variant="contained" disabled={busy} onClick={() => run(() => choose("prepared"))} data-testid="approve-product">Use prepared image</Button>}
            <Button variant="outlined" disabled={busy} onClick={() => run(() => choose("original"))} data-testid="use-original-product">Use cropped original</Button>
            <Button disabled={busy} onClick={() => setActive(null)}>Choose another photo</Button>
          </Stack>}
        </>}
      </Stack></DialogContent>
      <DialogActions><Button disabled={busy} onClick={() => setOpen(false)}>Close</Button></DialogActions>
    </Dialog>
  </Box>;
}
