import { CssBaseline } from "@mui/material";
import { createTheme, ThemeProvider } from "@mui/material/styles";

// MUI Dashboard v9.4.0 shared-theme/AppTheme: CSS-variable color schemes,
// component defaults and a single provider for every surface.
const theme = createTheme({
  cssVariables: { colorSchemeSelector: "data-mui-color-scheme" },
  colorSchemes: {
    light: { palette: { primary: { main: "#6D42E5" }, secondary: { main: "#087F83" }, background: { default: "#F5F4FA", paper: "#FFFFFF" }, text: { primary: "#252238", secondary: "#706D83" }, divider: "#E7E3F0" } },
    dark: { palette: { primary: { main: "#BCA7FF" }, secondary: { main: "#79D9D2" }, background: { default: "#171521", paper: "#242032" }, text: { primary: "#F5F1FF", secondary: "#BDB5D0" }, divider: "#433A59" } },
  },
  typography: { fontFamily: '"Inter", system-ui, sans-serif', h1: { fontSize: "2.75rem", fontWeight: 700, letterSpacing: "-.045em" }, h2: { fontSize: "1.6rem", fontWeight: 600 }, button: { textTransform: "none", fontWeight: 600 } },
  shape: { borderRadius: 18 },
  components: {
    MuiCard: { defaultProps: { variant: "elevation", elevation: 0 }, styleOverrides: { root: ({ theme }) => ({
      border: 0, borderRadius: 24, backgroundImage: "none",
      boxShadow: "0 2px 6px rgba(65, 38, 120, 0.04), 0 12px 36px -10px rgba(65, 38, 120, 0.14)",
      ...theme.applyStyles("dark", { boxShadow: "0 2px 6px rgba(0,0,0,.18), 0 16px 40px -12px rgba(0,0,0,.4)" }),
    }) } },
    MuiCardContent: { styleOverrides: { root: { padding: 28, "&:last-child": { paddingBottom: 28 } } } },
    MuiCardActions: { styleOverrides: { root: { padding: "0 28px 24px" } } },
    MuiButton: { defaultProps: { disableElevation: true, size: "medium" }, styleOverrides: { root: ({ theme }) => ({
      minHeight: 46, borderRadius: 16, padding: "10px 18px", fontSize: "0.9rem", gap: 6,
      "&.MuiButton-contained.MuiButton-colorPrimary:not(.Mui-disabled)": { background: "linear-gradient(115deg, #6D42E5, #A04AE7)", color: "#fff", boxShadow: "0 6px 18px -6px rgba(109,66,229,.38)" },
      ...theme.applyStyles("dark", { "&.MuiButton-contained.MuiButton-colorPrimary:not(.Mui-disabled)": { background: "linear-gradient(115deg, #BCA7FF, #DEB1F4)", color: "#211638" } }),
    }) } },
    MuiTextField: { defaultProps: { variant: "outlined", size: "medium" } },
    MuiOutlinedInput: { styleOverrides: { root: { borderRadius: 16 }, notchedOutline: { borderColor: "var(--mui-palette-divider)" } } },
    MuiListItemButton: { styleOverrides: { root: { borderRadius: 16, marginBottom: 8, paddingTop: 13, paddingBottom: 13 } } },
  },
});

export default function AppTheme({ children }) {
  return <ThemeProvider theme={theme} defaultMode="light" modeStorageKey="merakify-color-mode" disableTransitionOnChange>
    <CssBaseline enableColorScheme />{children}
  </ThemeProvider>;
}
