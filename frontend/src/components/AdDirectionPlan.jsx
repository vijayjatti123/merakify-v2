import { Box, Typography } from "@mui/material";

export function AdDirectionPlan({ direction, shots = [] }) {
  if (!direction) return null;
  return <Box data-testid="ad-direction-plan" sx={{ mx: 2, mb: 2, p: 2, borderRadius: 3, bgcolor: "action.hover" }}>
    <Typography fontWeight={700}>How your ad will unfold</Typography>
    <Typography variant="body2" sx={{ mt: 0.5 }}>{direction.takeaway}</Typography>
    {shots.length > 0 && <Box component="ol" aria-label="Visual story sequence" sx={{ display: "flex", gap: 1, overflowX: "auto", listStyle: "none", p: 0, my: 2 }}>
      {shots.map((shot) => <Box component="li" key={shot.shot_number} sx={{ minWidth: 190, flex: "1 0 190px", p: 1.5, borderRadius: 2, bgcolor: "background.paper" }}>
        <Typography variant="caption" color="primary.main">Shot {shot.shot_number} · {shot.duration_sec}s</Typography>
        <Typography variant="body2" fontWeight={600}>{shot.shot_direction?.purpose || shot.description}</Typography>
        {shot.shot_direction?.critical_outcome && <Typography variant="body2" sx={{ mt: 0.5 }}>Must see: {shot.shot_direction.critical_outcome}</Typography>}
      </Box>)}
    </Box>}
    <Box component="details">
      <Typography component="summary" variant="body2" sx={{ cursor: "pointer", color: "primary.main" }}>Look, pacing and sound plan</Typography>
      {[["Look and feel", direction.visual_approach], ["Pacing", direction.pacing],
        ["Sound plan · music has not been added", direction.sound_direction]].map(([label, value]) =>
        <Box key={label} sx={{ mt: 1.5 }}><Typography variant="caption" color="text.secondary">{label}</Typography>
          <Typography variant="body2">{value}</Typography></Box>)}
    </Box>
  </Box>;
}

export function ShotDirectionPlan({ shot }) {
  if (!shot.shot_direction) return null;
  return <Box component="details" data-testid={`shot-direction-${shot.shot_number}`} sx={{ my: 1.5 }}>
    <Typography component="summary" variant="body2" sx={{ cursor: "pointer", color: "primary.main" }}>What happens in this shot</Typography>
    {[["Speaker", shot.has_dialogue ? (shot.speech_mode === "voiceover" ? "Narrator · off screen" : shot.speaker_name || "Choose a speaker before approval") : "No speech"], ["Transition", shot.transition_after || "cut"], ["Framing", shot.camera_angle], ["Camera movement", shot.camera_movement], ["Lens", shot.lens], ["Lighting", shot.lighting], ["Planned duration", `${shot.duration_sec}s`], ["Why this shot", shot.shot_direction.purpose], ["Starts with", shot.state_at_shot_start],
      ...(Array.isArray(shot.opening_characters) ? [["Visible at the start", shot.opening_characters.join(', ') || 'No visible characters']] : []),
      ["Performance", shot.shot_direction.performance], ["Product and props", shot.shot_direction.product_props],
      ...(shot.shot_direction.blocking ? [["Where everyone is", shot.shot_direction.blocking]] : []),
      ...(shot.shot_direction.action_beats || []).map((beat, i) => [`Action ${i + 1}`, beat]),
      ...(shot.shot_direction.critical_outcome ? [["What the viewer must see", shot.shot_direction.critical_outcome]] : []),
      ["Ends with", shot.state_at_shot_end], ["Next cut", shot.shot_direction.edit_intent]].map(([label, text]) =>
      <Box key={label} sx={{ mt: 1 }}><Typography variant="caption" color="text.secondary">{label}</Typography>
        <Typography variant="body2">{text}</Typography></Box>)}
  </Box>;
}
