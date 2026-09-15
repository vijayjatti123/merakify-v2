import { useState } from "react";
import { AppBar, Box, Button, Divider, Drawer, IconButton, List, ListItem, ListItemButton, ListItemIcon, ListItemText, Stack, Toolbar, Tooltip, Typography } from "@mui/material";
import { useColorScheme } from "@mui/material/styles";
import { Clapperboard, Home, Users, History, Menu, Moon, Sun, Plus } from "lucide-react";

// Structure adapted from MUI v9.4.0 Dashboard: SideMenu, MenuContent,
// AppNavbar and SideMenuMobile. No unrelated dashboard/chart functionality.
const drawerWidth = 240;
const items = [
  { key: "director", label: "Home / New Job", Icon: Home },
  { key: "vault", label: "Character Vault", Icon: Users },
  { key: "history", label: "Job History", Icon: History },
];

function ColorModeToggle() {
  const { mode, setMode } = useColorScheme();
  const dark = mode === "dark";
  return <Tooltip title={dark ? "Switch to light mode" : "Switch to dark mode"}>
    <IconButton aria-label={dark ? "Switch to light mode" : "Switch to dark mode"} onClick={() => setMode(dark ? "light" : "dark")}>
      {dark ? <Sun size={20} /> : <Moon size={20} />}
    </IconButton>
  </Tooltip>;
}

export default function DashboardLayout({ screen, onNavigate, onNew, children }) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const navigation = <>
    <Stack direction="row" spacing={1.5} sx={{ p: 3, alignItems: "center" }}>
      <Clapperboard size={25} /><Box><Typography fontWeight={700}>Merakify</Typography><Typography variant="caption" color="text.secondary">Your video workspace</Typography></Box>
    </Stack>
    <Divider />
    <Box component="nav" aria-label="Workspace navigation" sx={{ p: 2, flex: 1 }}>
      <List>{items.map(({ key, label, Icon }) => <ListItem key={key} disablePadding>
        <ListItemButton selected={screen === key} onClick={() => { onNavigate(key); setMobileOpen(false); }}>
          <ListItemIcon sx={{ minWidth: 36 }}><Icon size={20} /></ListItemIcon><ListItemText primary={label} />
        </ListItemButton>
      </ListItem>)}</List>
    </Box>
    <Divider /><Stack direction="row" sx={{ p: 2, alignItems: "center", justifyContent: "space-between" }}>
      <Typography variant="body2" color="text.secondary">Appearance</Typography><ColorModeToggle />
    </Stack>
  </>;
  return <Box sx={{ display: "flex", minHeight: "100vh" }}>
    <Drawer variant="permanent" sx={{ width: drawerWidth, flexShrink: 0, display: { xs: "none", md: "block" }, "& .MuiDrawer-paper": { width: drawerWidth, boxSizing: "border-box", bgcolor: "background.paper" } }}>{navigation}</Drawer>
    <AppBar position="fixed" color="inherit" elevation={0} sx={{ display: { xs: "block", md: "none" }, borderBottom: 1, borderColor: "divider" }}>
      <Toolbar><IconButton aria-label="Open navigation" edge="start" onClick={() => setMobileOpen(true)}><Menu /></IconButton><Typography sx={{ flex: 1, ml: 1 }} fontWeight={700}>Merakify</Typography><ColorModeToggle /></Toolbar>
    </AppBar>
    <Drawer variant="temporary" open={mobileOpen} onClose={() => setMobileOpen(false)} sx={{ display: { xs: "block", md: "none" }, "& .MuiDrawer-paper": { width: drawerWidth } }}>{navigation}</Drawer>
    <Box component="main" id="main-content" sx={{ flex: 1, minWidth: 0, bgcolor: "background.default", px: { xs: 2, md: 4 }, pb: 5, pt: { xs: 10, md: 3 } }}>
      <Stack direction="row" sx={{ mb: 4, alignItems: "center", justifyContent: "space-between" }}>
        <Typography color="text.secondary" variant="body2">Workspace / {screen === "vault" ? "Character Vault" : screen === "history" ? "Job History" : "Create"}</Typography>
        <Button startIcon={<Plus size={16} />} onClick={onNew}>New job</Button>
      </Stack>
      <Box sx={{ maxWidth: 1440, mx: "auto" }}>{children}</Box>
    </Box>
  </Box>;
}
