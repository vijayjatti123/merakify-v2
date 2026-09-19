import { Children, isValidElement, useId, useState } from "react";
import { Button, Menu, MenuItem, ListSubheader, Typography } from "@mui/material";
import { Check, ChevronDown } from "lucide-react";

function optionsFrom(children, group = "") {
  return Children.toArray(children).flatMap(child => {
    if (!isValidElement(child)) return [];
    if (child.type === "optgroup") return optionsFrom(child.props.children, child.props.label);
    return [{ value: child.props.value ?? child.props.children, label: child.props.children, group }];
  });
}

// Keep the existing native-option values and change-event contract.
export default function StudioSelect({ label, note, value, onChange, children }) {
  const [anchor, setAnchor] = useState(null);
  const id = useId();
  const options = optionsFrom(children);
  const selected = options.find(option => option.value === value);
  return <div className="studio-parameter">
    <Button type="button" color="inherit" aria-label={`${label}: ${selected?.label || value}`} aria-haspopup="menu"
      aria-expanded={Boolean(anchor)} aria-controls={anchor ? id : undefined} onClick={event => setAnchor(event.currentTarget)} endIcon={<ChevronDown size={13} />}>
      <span className="studio-parameter-label">{label}</span><span className="studio-parameter-value">{selected?.label || value}</span>
    </Button>
    <Menu id={id} anchorEl={anchor} open={Boolean(anchor)} onClose={() => setAnchor(null)}
      slotProps={{ list: { "aria-label": label }, paper: { sx: { minWidth: 220, maxWidth: "min(380px, calc(100vw - 32px))", maxHeight: 400 } } }}>
      <ListSubheader sx={{ bgcolor: "transparent", lineHeight: "36px", fontSize: 11 }}>{label}</ListSubheader>
      {options.flatMap((option, index) => [
        option.group && option.group !== options[index - 1]?.group ? <ListSubheader key={`group-${index}`} sx={{ bgcolor: "transparent", fontSize: 11, lineHeight: "32px" }}>{option.group}</ListSubheader> : null,
        <MenuItem key={String(option.value)} selected={value === option.value} onClick={() => { onChange({ target: { value: option.value } }); setAnchor(null); }}>
          <span style={{ flex: 1, whiteSpace: "normal", paddingRight: 16 }}>{option.label}</span>{value === option.value && <Check size={15} />}
        </MenuItem>,
      ])}
      {note && <Typography component="li" role="none" variant="caption" sx={{ px: 2, py: 1, maxWidth: 320, color: "text.secondary", display: "block" }}>{note}</Typography>}
    </Menu>
  </div>;
}
