import { Box, Divider, TextField, MenuItem, Typography } from "@mui/material";

export function editablePlan(shot) {
  const fields = ["description", "dialogue_text", "scene_number", "duration_sec", "camera_angle", "camera_direction", "lens", "lighting", "composition_note", "state_at_shot_start", "state_at_shot_end", "shot_direction", "opening_characters", "speaker_name", "transition_after", "speech_mode", "characters_in_shot"];
  return Object.fromEntries(fields.filter(k => shot[k] != null).map(k => [k, shot[k]]));
}

export default function DirectorPlanEditor({ value, onChange, shotNumber, cast = [], speaking = false }) {
  const set = (key, v) => onChange(current => ({ ...current, [key]: v }));
  const text = (key, label, multiline = true) => <TextField key={key} label={label} value={value[key] ?? ""}
    multiline={multiline} minRows={multiline ? 2 : undefined} fullWidth
    slotProps={{ htmlInput: { "data-testid": `shot-${shotNumber}-${key}` } }}
    onChange={e => set(key, e.target.value)} />;
  const grid = { display: "grid", gridTemplateColumns: { xs: "1fr", md: "1fr 1fr" }, gap: 2 };
  return <Box sx={{ my: 2 }} data-testid={`director-editor-${shotNumber}`}>
    <Typography variant="body2" sx={{ mb: 2 }}>Describe what the viewer sees and hears. Saving only updates the plan; it does not generate media.</Typography>
    <Box sx={grid}>
    <TextField select label="Speech in this shot" value={value.speech_mode || (speaking ? "onscreen" : "none")} onChange={e => set("speech_mode", e.target.value)}>
      <MenuItem value="none">No speech</MenuItem><MenuItem value="onscreen">A visible character speaks</MenuItem><MenuItem value="voiceover">Narration over the scene</MenuItem>
    </TextField>
    {["characters_in_shot", "opening_characters"].map(key => <TextField key={key} select slotProps={{ select: { multiple: true } }} label={key === "opening_characters" ? "Visible in the opening image" : "Characters visible during this shot"} value={value[key] || []} onChange={e => set(key, e.target.value)}>
      {cast.map(name => <MenuItem key={name} value={name}>{name}</MenuItem>)}
    </TextField>)}
    {(value.speech_mode === "onscreen" || (!value.speech_mode && speaking)) && <TextField select label="Who speaks this line?" value={value.speaker_name || ""} onChange={e => set("speaker_name", e.target.value)}>
      {(value.characters_in_shot || []).map(name => <MenuItem key={name} value={name}>{name}</MenuItem>)}
    </TextField>}
    {text("description", "What happens in this shot?")}
    {text("dialogue_text", "Exact words spoken · leave empty if nobody speaks")}
    <TextField label="Planned seconds" type="number" value={value.duration_sec ?? ""} onChange={e => set("duration_sec", Number(e.target.value))} />
    {text("state_at_shot_start", "What is visible at the start?")}{text("state_at_shot_end", "What is visible at the end?")}
    {Object.entries({purpose:"Why is this shot needed?",performance:"How should the character behave?"}).map(([key,label]) =>
      <TextField key={key} label={label} multiline minRows={2} value={value.shot_direction?.[key] || ""} onChange={e => set("shot_direction", {...value.shot_direction,[key]:e.target.value})} />)}
    {value.shot_direction?.action_beats && <>
      {[['blocking', 'Where is everyone?'], ['support_and_contact', 'What is each person or object standing, sitting on, or holding?'], ['critical_outcome', 'What must the viewer clearly see?']].map(([key, label]) =>
        <TextField key={key} label={label} multiline minRows={2} value={value.shot_direction[key] || ''}
          slotProps={{htmlInput: {'data-testid': `edit-${key}-${shotNumber}`}}}
          onChange={e => set('shot_direction', {...value.shot_direction, [key]: e.target.value})} />)}
      {value.shot_direction.action_beats.map((beat, i) => <TextField key={`beat-${i}`} label={`Then ${i + 1}`}
        multiline minRows={2} value={beat} slotProps={{htmlInput: {'data-testid': `edit-action-${i + 1}-${shotNumber}`}}}
        onChange={e => set('shot_direction', {...value.shot_direction,
          action_beats: value.shot_direction.action_beats.map((text, n) => n === i ? e.target.value : text)})} />)}
    </>}
    </Box>
    <Box component="details" data-testid={`advanced-shot-controls-${shotNumber}`} sx={{ mt: 2, p: 2, border: 1, borderColor: "divider", borderRadius: 2 }}>
      <Box component="summary" sx={{ cursor: "pointer", fontWeight: 600 }}>Advanced camera and continuity controls</Box>
      <Typography variant="body2" color="text.secondary" sx={{ my: 2 }}>These settings are optional. Change them only when you need precise camera or continuity control.</Typography>
      <Divider sx={{ mb: 2 }} />
      <Box sx={grid}>
    <TextField select label="Transition to next shot" value={value.transition_after || "cut"} onChange={e => set("transition_after", e.target.value)}>
      {["cut", "crossfade", "match cut"].map(type => <MenuItem key={type} value={type}>{type}</MenuItem>)}
    </TextField>
    <TextField label="Scene number" type="number" value={value.scene_number ?? ""} onChange={e => set("scene_number", Number(e.target.value))} />
    {text("camera_angle", "Camera framing and angle")}{text("lens", "Focus and depth")}{text("lighting", "Lighting")}{text("composition_note", "Where subjects appear in the frame")}
    {Object.entries({ movement: ["hold","dolly","truck","pan","tilt","track","orbit","crane","pedestal","zoom","dolly_zoom","roll"], direction: ["none","in","out","left","right","lateral","up","down","clockwise","counterclockwise","follow"], speed: ["none","slow","normal","fast","whip"], stabilization: ["locked","smooth","handheld"] }).map(([key, options]) =>
      <TextField key={key} select label={({movement:"Camera movement", direction:"Movement direction", speed:"Movement speed", stabilization:"Camera steadiness"})[key]} value={value.camera_direction?.[key] ?? ""} onChange={e => set("camera_direction", {...value.camera_direction, [key]: e.target.value})}>
        {options.map(option => <MenuItem key={option} value={option}>{option.replaceAll("_", " ")}</MenuItem>)}
      </TextField>)}
    {Object.entries({product_props:"Products and important objects",edit_intent:"How this shot connects to the next"}).map(([key,label]) =>
      <TextField key={key} label={label} multiline minRows={2} value={value.shot_direction?.[key] || ""} onChange={e => set("shot_direction", {...value.shot_direction,[key]:e.target.value})} />)}
    {value.shot_direction?.action_beats && <>
      {[["entry_exit_paths", "How people move into or out of the space"], ["spatial_invariants", "Positions that must stay consistent"], ["forbidden_geometry", "Visual mistakes to avoid"]].map(([key, label]) =>
        <TextField key={key} label={`${label} · one per line`} multiline minRows={2}
          value={(value.shot_direction[key] || []).join("\n")}
          slotProps={{htmlInput: {"data-testid": `edit-${key}-${shotNumber}`}}}
          onChange={e => set("shot_direction", {...value.shot_direction,
            [key]: e.target.value.split("\n").map(line => line.trim()).filter(Boolean)})} />)}
    </>}
      </Box>
    </Box>
  </Box>;
}
