import { Box, Typography } from "@mui/material";

export function AdDirectionPlan({ direction }) {
  if (!direction) return null;
  return <Box component="details" data-testid="ad-direction-plan" sx={{ mx: 2, mb: 2, p: 2, borderRadius: 3, bgcolor: "action.hover" }}>
    <Typography component="summary" sx={{ cursor: "pointer", fontWeight: 600 }}>How your story will come to life</Typography>
    {[["What viewers should take away", direction.takeaway], ["Look and feel", direction.visual_approach],
      ["Pacing", direction.pacing], ["Sound plan · music has not been added", direction.sound_direction]].map(([label, text]) =>
      <Box key={label} sx={{ mt: 1.5 }}><Typography variant="caption" color="text.secondary">{label}</Typography>
        <Typography variant="body2">{text}</Typography></Box>)}
  </Box>;
}

export function ShotDirectionPlan({ shot, expanded = false }) {
  if (!shot.shot_direction) return null;
  return <Box component="details" open={expanded || undefined} data-testid={`shot-direction-${shot.shot_number}`} sx={{ my: 1.5 }}>
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
