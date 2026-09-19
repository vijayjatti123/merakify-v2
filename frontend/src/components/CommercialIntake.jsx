import { Box, Button, TextField, Typography } from "@mui/material";
import { Users, Package, Orbit, Smartphone, Check } from "lucide-react";
import StudioSelect from "./StudioSelect";

export const AD_TYPES = [
  { id: "character", label: "Character Commercial", icon: Users, description: "A story told through your characters", treatment: "Performance & mood", example: "A warm reunion, a playful argument, a quiet moment…" },
  { id: "product", label: "Product Commercial", icon: Package, description: "Put your product in the spotlight", treatment: "How should we show it?", example: "A studio reveal, close-up details, a practical demonstration…" },
  { id: "cgi", label: "CGI Commercial", icon: Orbit, description: "Imagine a world around your product", treatment: "Visual concept & transformation", example: "A bottle rises from a pool of light; its shape and label stay intact…" },
  { id: "ugc", label: "UGC Commercial", icon: Smartphone, description: "Natural, creator-style storytelling", treatment: "Creator style & setting", example: "A relaxed phone demo at home, direct to camera…" },
];

export default function CommercialIntake({ adType, onType, value, onChange, disabled }) {
  const active = AD_TYPES.find(type => type.id === adType) || AD_TYPES[0];
  const field = (key, label, placeholder) => <TextField key={key} size="small" fullWidth
    label={label} placeholder={placeholder} value={value[key] || ""} disabled={disabled}
    inputProps={{ maxLength: key === "treatment" ? 1500 : key === "selling_point" || key === "must_preserve" ? 1000 : 500,
      "data-testid": `commercial-${key}` }}
    onChange={event => onChange({ ...value, [key]: event.target.value })} />;
  return <Box data-testid="commercial-intake">
    <Box role="group" aria-label="Commercial type" sx={{ display: "grid", gridTemplateColumns: { xs: "1fr 1fr", lg: "repeat(4, 1fr)" }, gap: 1.5 }}>
      {AD_TYPES.map(({ id, label, icon: Icon, description }) => <Button key={id} type="button"
        data-testid={`ad-type-${id}`} aria-pressed={adType === id} disabled={disabled} onClick={() => onType(id)}
        variant="outlined" color={adType === id ? "primary" : "inherit"}
        sx={{ p: 2, textAlign: "left", alignItems: "flex-start", flexDirection: "column", gap: 1,
          borderColor: adType === id ? "primary.main" : "divider", bgcolor: adType === id ? "action.selected" : "transparent" }}>
        <Box sx={{ display: "flex", width: "100%", justifyContent: "space-between" }}><Icon size={22} />{adType === id && <Check size={16} />}</Box>
        <Typography fontWeight={600}>{label}</Typography>
        <Typography variant="caption" color="text.secondary">{description}</Typography>
      </Button>)}
    </Box>
    <Typography variant="body2" color="text.secondary" sx={{ mt: 1.5, mb: 2 }} role="status" data-testid="commercial-mode-note">
      {adType === "cgi" ? "AI-generated CGI-style visuals. Keep the product recognizable; describe what changes around it."
        : adType === "ugc" ? "AI creator-style content. Bring a product or describe a service — no product upload required for services."
        : adType === "product" ? "Add an approved product photo below, then tell us what makes it worth noticing."
        : "Your familiar story workflow, with consistent characters and voices."}
    </Typography>
    <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", md: "1fr 1fr" }, gap: 2 }}>
      {field("selling_point", "Main message (optional)", "The one thing viewers should remember")}
      {field("treatment", `${active.treatment} (optional)`, active.example)}
    </Box>
    <details style={{ marginTop: 16 }} data-testid="commercial-details">
      <summary style={{ cursor: "pointer" }}>Audience, call to action & sound</summary>
      <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", md: "1fr 1fr" }, gap: 2, mt: 2 }}>
        {field("audience", "Audience", "Who is this for?")}
        {field("call_to_action", "Call to action", "What should viewers do next?")}
        {field("must_preserve", "Must keep / must avoid", "Exact claims, packaging details or exclusions")}
        <StudioSelect label="Speech" value={value.audio_mode || "auto"}
          onChange={event => onChange({ ...value, audio_mode: event.target.value })}>
          <option value="auto">Follow my story</option><option value="silent">No speech</option>
          <option value="voiceover">Narration over visuals</option><option value="onscreen">On-screen speaker</option>
        </StudioSelect>
      </Box>
    </details>
  </Box>;
}
