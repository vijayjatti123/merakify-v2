import { Box, TextField, MenuItem, Typography } from "@mui/material";

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
  return <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", md: "1fr 1fr" }, gap: 2, my: 2 }} data-testid={`director-editor-${shotNumber}`}>
    <Typography variant="body2" sx={{ gridColumn: "1 / -1" }}>Review the action, speech and opening/end states together. Saving does not generate media.</Typography>
    <TextField select label="Speech in this shot" value={value.speech_mode || (speaking ? "onscreen" : "none")} onChange={e => set("speech_mode", e.target.value)}>
      <MenuItem value="none">No speech</MenuItem><MenuItem value="onscreen">A visible character speaks</MenuItem><MenuItem value="voiceover">Narration over the scene</MenuItem>
    </TextField>
    {["characters_in_shot", "opening_characters"].map(key => <TextField key={key} select slotProps={{ select: { multiple: true } }} label={key === "opening_characters" ? "Visible in the opening image" : "Characters visible during this shot"} value={value[key] || []} onChange={e => set(key, e.target.value)}>
      {cast.map(name => <MenuItem key={name} value={name}>{name}</MenuItem>)}
    </TextField>)}
    {(value.speech_mode === "onscreen" || (!value.speech_mode && speaking)) && <TextField select label="Who speaks this line?" value={value.speaker_name || ""} onChange={e => set("speaker_name", e.target.value)}>
      {(value.characters_in_shot || []).map(name => <MenuItem key={name} value={name}>{name}</MenuItem>)}
    </TextField>}
    <TextField select label="Transition to next shot" value={value.transition_after || "cut"} onChange={e => set("transition_after", e.target.value)}>
      {["cut", "crossfade", "match cut"].map(type => <MenuItem key={type} value={type}>{type}</MenuItem>)}
    </TextField>
    {text("description", "Action and story")}
    {text("dialogue_text", "Complete spoken line · keep empty for silent shots")}
    <TextField label="Scene number" type="number" value={value.scene_number ?? ""} onChange={e => set("scene_number", Number(e.target.value))} />
    <TextField label="Planned seconds" type="number" value={value.duration_sec ?? ""} onChange={e => set("duration_sec", Number(e.target.value))} />
    {text("state_at_shot_start", "Opening image")}{text("state_at_shot_end", "Ending image")}
    {text("camera_angle", "Framing and angle")}{text("lens", "Lens and depth")}{text("lighting", "Lighting")}{text("composition_note", "Composition")}
    {Object.entries({ movement: ["hold","dolly","truck","pan","tilt","track","orbit","crane","pedestal","zoom","dolly_zoom","roll"], direction: ["none","in","out","left","right","lateral","up","down","clockwise","counterclockwise","follow"], speed: ["none","slow","normal","fast","whip"], stabilization: ["locked","smooth","handheld"] }).map(([key, options]) =>
      <TextField key={key} select label={`Camera ${key}`} value={value.camera_direction?.[key] ?? ""} onChange={e => set("camera_direction", {...value.camera_direction, [key]: e.target.value})}>
        {options.map(option => <MenuItem key={option} value={option}>{option.replaceAll("_", " ")}</MenuItem>)}
      </TextField>)}
    {Object.entries({purpose:"Purpose",performance:"Character performance",product_props:"Product and props",edit_intent:"Edit and transition intention"}).map(([key,label]) =>
      <TextField key={key} label={label} multiline minRows={2} value={value.shot_direction?.[key] || ""} onChange={e => set("shot_direction", {...value.shot_direction,[key]:e.target.value})} />)}
  </Box>;
}
