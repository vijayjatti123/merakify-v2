import { CssBaseline } from "@mui/material";
import { createTheme, ThemeProvider } from "@mui/material/styles";

// Shared tokens include portaled dialogs and menus.
const theme = createTheme({
  cssVariables: true,
  palette: {
    mode: "dark",
    primary: { main: "#5b9dff", contrastText: "#04101d" },
    secondary: { main: "#ff7ab8", contrastText: "#220d17" },
    background: { default: "#07080a", paper: "#0f1116" },
    text: { primary: "#f4f6f8", secondary: "#8f98a5" },
    divider: "#262b34", success: { main: "#77d6ad" },
    warning: { main: "#ffc061" }, error: { main: "#ff7a7a" },
  },
  typography: {
    fontFamily: '"Inter", system-ui, sans-serif', fontSize: 13,
    h1: { fontSize: "2rem", fontWeight: 600, letterSpacing: "-.04em" },
    h2: { fontSize: "1.35rem", fontWeight: 600, letterSpacing: "-.025em" },
    button: { textTransform: "none", fontWeight: 550 },
  },
  shape: { borderRadius: 12 },
  components: {
    MuiPaper: { styleOverrides: { root: { backgroundImage: "none" } } },
    MuiCard: { defaultProps: { elevation: 0 }, styleOverrides: { root: { border: "1px solid #262b34", borderRadius: 16, boxShadow: "none" } } },
    MuiCardContent: { styleOverrides: { root: { padding: 20, "&:last-child": { paddingBottom: 20 } } } },
    MuiCardActions: { styleOverrides: { root: { padding: "0 20px 20px" } } },
    MuiButton: { defaultProps: { disableElevation: true }, styleOverrides: { root: { minHeight: 38, borderRadius: 10, padding: "8px 14px", fontSize: ".8125rem", gap: 4 } } },
    MuiTextField: { defaultProps: { variant: "outlined", size: "small" } },
    MuiOutlinedInput: { styleOverrides: { root: { borderRadius: 10, background: "#0a0c0f" }, notchedOutline: { borderColor: "#262b34" } } },
    MuiMenu: { defaultProps: { slotProps: { paper: { sx: { backgroundColor: "#15181f", border: "1px solid #262b34", mt: .75, boxShadow: "0 16px 48px #0009" } } } } },
    MuiMenuItem: { styleOverrides: { root: { fontSize: 13, minHeight: 40, margin: "2px 6px", borderRadius: 7 } } },
    MuiDialog: { styleOverrides: { paper: { border: "1px solid #262b34", borderRadius: 16 } } },
    MuiChip: { styleOverrides: { root: { borderRadius: 7, fontSize: 11, fontWeight: 500 } } },
    MuiAlert: { styleOverrides: { root: { borderRadius: 10 } } },
    MuiListItemButton: { styleOverrides: { root: { borderRadius: 10, marginBottom: 4, paddingTop: 11, paddingBottom: 11 } } },
  },
});

export default function AppTheme({ children }) {
  return <ThemeProvider theme={theme}><CssBaseline enableColorScheme />{children}</ThemeProvider>;
}
