import { Box, MenuItem, TextField, Typography } from "@mui/material";

// The saved shot remains rich and structured. The customer edits only its
// creative intent; the Director refreshes execution fields before saving.
export function editablePlan(shot) {
  return {
    description: shot.description || "",
    dialogue_text: shot.dialogue_text || "",
    speech_mode: shot.speech_mode || (shot.has_dialogue ? "onscreen" : "none"),
    speaker_name: shot.speaker_name || "",
    characters_in_shot: shot.characters_in_shot || [],
    duration_sec: shot.duration_sec,
  };
}

export default function DirectorPlanEditor({ value, onChange, shotNumber, cast = [], issue = "" }) {
  const set = (key, next) => onChange(current => ({ ...current, [key]: next }));
  const speaking = value.speech_mode !== "none";
  return <Box sx={{ my: 2 }} data-testid={`director-editor-${shotNumber}`}>
    <Typography variant="body2" sx={{ mb: 2 }}>
      Describe this shot in your own words. We’ll update its camera, movement and timing instructions for you.
    </Typography>
    {issue && <Typography variant="body2" color="warning.main" sx={{ mb: 2 }}>
      What needs attention: {issue}
    </Typography>}
    <TextField autoFocus fullWidth multiline minRows={5} label="What should happen in this shot?"
      value={value.description} onChange={event => set("description", event.target.value)}
      helperText="Include where people are, what happens first, and what the viewer should see at the end."
      slotProps={{ htmlInput: { "data-testid": `shot-${shotNumber}-description`, maxLength: 2000 } }} />
    <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", md: "1fr 1fr" }, gap: 2, mt: 2 }}>
      <TextField select label="Speech" value={value.speech_mode} onChange={event => {
        const mode = event.target.value;
        onChange(current => ({ ...current, speech_mode: mode,
          dialogue_text: mode === "none" ? "" : current.dialogue_text,
          speaker_name: mode === "none" ? "" : current.speaker_name }));
      }}>
        <MenuItem value="none">No speech</MenuItem>
        <MenuItem value="onscreen">A character speaks</MenuItem>
        <MenuItem value="voiceover">Narration</MenuItem>
      </TextField>
      {value.speech_mode === "onscreen" && <TextField select label="Who speaks?" value={value.speaker_name}
        onChange={event => set("speaker_name", event.target.value)}>
        {(value.characters_in_shot || []).map(name => <MenuItem key={name} value={name}>{name}</MenuItem>)}
      </TextField>}
    </Box>
    {speaking && <TextField fullWidth multiline minRows={2} sx={{ mt: 2 }} label="Exact words spoken"
      value={value.dialogue_text} onChange={event => set("dialogue_text", event.target.value)}
      slotProps={{ htmlInput: { "data-testid": `shot-${shotNumber}-dialogue_text` } }} />}
    <Box component="details" sx={{ mt: 2 }} data-testid={`advanced-shot-controls-${shotNumber}`}>
      <Typography component="summary" variant="body2" sx={{ cursor: "pointer", color: "text.secondary" }}>
        More options
      </Typography>
      <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", md: "1fr 1fr" }, gap: 2, mt: 2 }}>
        <TextField select label="Characters in this shot" value={value.characters_in_shot || []}
          slotProps={{ select: { multiple: true } }}
          onChange={event => set("characters_in_shot", event.target.value)}>
          {cast.map(name => <MenuItem key={name} value={name}>{name}</MenuItem>)}
        </TextField>
        <TextField label="Planned seconds" type="number" value={value.duration_sec ?? ""}
          onChange={event => set("duration_sec", Number(event.target.value))} />
      </Box>
    </Box>
  </Box>;
}
