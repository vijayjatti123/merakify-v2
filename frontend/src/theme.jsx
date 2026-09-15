import { CssBaseline } from "@mui/material";
import { createTheme, ThemeProvider } from "@mui/material/styles";

// MUI Dashboard v9.4.0 shared-theme/AppTheme: CSS-variable color schemes,
// component defaults and a single provider for every surface.
const theme = createTheme({
  cssVariables: { colorSchemeSelector: "data-mui-color-scheme" },
  colorSchemes: {
    light: { palette: { primary: { main: "#9B4B26" }, background: { default: "#FAF7F2", paper: "#FFFFFF" }, text: { primary: "#302C27", secondary: "#6E675E" }, divider: "#E7E0D7" } },
    dark: { palette: { primary: { main: "#E7AF83" }, background: { default: "#191B20", paper: "#23262D" }, text: { primary: "#F3EEE7", secondary: "#B8B4AD" }, divider: "#41434B" } },
  },
  typography: { fontFamily: '"Inter", system-ui, sans-serif', h1: { fontSize: "2.25rem", fontWeight: 600 }, h2: { fontSize: "1.6rem", fontWeight: 600 }, button: { textTransform: "none", fontWeight: 600 } },
  shape: { borderRadius: 12 },
  components: {
    MuiCard: { defaultProps: { variant: "outlined" } },
    MuiButton: { defaultProps: { disableElevation: true } },
    MuiTextField: { defaultProps: { variant: "outlined", size: "small" } },
    MuiListItemButton: { styleOverrides: { root: { borderRadius: 12, marginBottom: 4 } } },
  },
});

export default function AppTheme({ children }) {
  return <ThemeProvider theme={theme} defaultMode="light" modeStorageKey="merakify-color-mode" disableTransitionOnChange>
    <CssBaseline enableColorScheme />{children}
  </ThemeProvider>;
}
