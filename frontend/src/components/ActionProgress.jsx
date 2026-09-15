import { Box, LinearProgress, Typography } from "@mui/material";

export default function ActionProgress({ label, value }) {
  return <Box role="status" sx={{ width: "100%", my: 2 }}>
    <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>{label}</Typography>
    <LinearProgress aria-label={label} variant={Number.isFinite(value) ? "determinate" : "indeterminate"} value={value} />
  </Box>;
}
