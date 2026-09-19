import { Dialog, DialogTitle, DialogContent, IconButton, Stack, Typography, Tooltip } from "@mui/material";
import { ChevronLeft, ChevronRight, ExternalLink, X } from "lucide-react";

export default function PreviewLightbox({ shot, shots, onSelect, onClose }) {
  const previews = shots.filter(item => item.still_frame_url);
  const index = previews.findIndex(item => item.shot_number === shot?.shot_number);
  const current = previews[index] || shot;
  const move = offset => {
    if (previews.length > 1) onSelect(previews[(index + offset + previews.length) % previews.length]);
  };
  return <Dialog open={Boolean(shot)} onClose={onClose} maxWidth="lg" fullWidth aria-labelledby="preview-lightbox-title"
    onKeyDown={event => {
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault(); move(event.key === "ArrowRight" ? 1 : -1);
      }
    }}>
    <DialogTitle id="preview-lightbox-title" sx={{ display: "flex", alignItems: "center", gap: 2, fontSize: 15 }}>
      <span style={{ flex: 1 }}>Shot {current?.shot_number} · Opening preview</span>
      {current?.still_frame_url && <Tooltip title="Open original image"><IconButton component="a" href={current.still_frame_url} target="_blank" rel="noreferrer" aria-label="Open original image"><ExternalLink size={18} /></IconButton></Tooltip>}
      <IconButton aria-label="Close preview" onClick={onClose}><X size={20} /></IconButton>
    </DialogTitle>
    <DialogContent sx={{ px: { xs: 1.5, sm: 3 } }}>
      <img src={current?.still_frame_url} alt={current?.description || "Shot preview"} style={{ width: "100%", maxHeight: "65dvh", objectFit: "contain", borderRadius: 8, background: "#07080a" }} />
      <Stack direction="row" sx={{ alignItems: "center", gap: 1, my: 1.5 }}>
        <IconButton aria-label="Previous preview" disabled={previews.length < 2} onClick={() => move(-1)}><ChevronLeft size={20} /></IconButton>
        <Typography variant="caption" color="text.secondary" sx={{ flex: 1, textAlign: "center" }}>{index + 1} of {previews.length} previews</Typography>
        <IconButton aria-label="Next preview" disabled={previews.length < 2} onClick={() => move(1)}><ChevronRight size={20} /></IconButton>
      </Stack>
      <Typography variant="body2" color="text.secondary">{current?.description}</Typography>
    </DialogContent>
  </Dialog>;
}
