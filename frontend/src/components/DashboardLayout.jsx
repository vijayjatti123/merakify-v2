import { useState } from "react";
import { Box, Button, Divider, Drawer, IconButton, List, ListItem, ListItemButton, ListItemIcon, ListItemText, Stack, Tooltip, Typography } from "@mui/material";
import { Clapperboard, Users, Grid2X2, Menu, Plus, PanelLeftClose, PanelLeftOpen, Video } from "lucide-react";

const items = [
  { key: "director", label: "Create", Icon: Video },
  { key: "vault", label: "Character Vault", Icon: Users },
  { key: "history", label: "My projects", Icon: Grid2X2 },
];

export default function DashboardLayout({ screen, onNavigate, onNew, children }) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem("merakify-studio-rail") === "collapsed"; } catch { return false; }
  });
  const width = collapsed ? 68 : 236;
  function toggleRail() {
    setCollapsed(value => {
      try { localStorage.setItem("merakify-studio-rail", value ? "expanded" : "collapsed"); } catch { /* Storage is optional. */ }
      return !value;
    });
  }
  const navigation = (compact = false, mobile = false) => <>
    <Stack direction="row" sx={{ height: 68, px: 2, gap: 1.5, alignItems: "center", flexShrink: 0 }}>
      <span className="studio-brand"><Clapperboard size={21} /></span>
      {!compact && <Typography fontWeight={650} fontSize={19} letterSpacing="-.6px">merakify<span className="studio-brand-dot">.</span></Typography>}
    </Stack>
    <Divider />
    <Box component="nav" aria-label={mobile ? "Mobile workspace navigation" : "Workspace navigation"} sx={{ p: 1.25, pt: 2.5, flex: 1 }}>
      <List disablePadding>{items.map(({ key, label, Icon }) => <ListItem key={key} disablePadding>
        <Tooltip title={compact ? label : ""} placement="right">
          <ListItemButton aria-label={label} aria-current={screen === key ? "page" : undefined} selected={screen === key}
            onClick={() => { onNavigate(key); setMobileOpen(false); }} sx={{ px: compact ? 1.5 : 1.75, minHeight: 44 }}>
            <ListItemIcon sx={{ minWidth: compact ? 20 : 34 }}><Icon size={18} /></ListItemIcon>
            {!compact && <ListItemText primary={label} slotProps={{ primary: { sx: { fontSize: 13, fontWeight: 500 } } }} />}
          </ListItemButton>
        </Tooltip>
      </ListItem>)}</List>
    </Box>
    {!mobile && <Box sx={{ p: 1.25, borderTop: 1, borderColor: "divider" }}>
      <Tooltip title={collapsed ? "Expand navigation" : "Collapse navigation"} placement="right">
        <Button fullWidth color="inherit" aria-label={collapsed ? "Expand navigation" : "Collapse navigation"} onClick={toggleRail}
          sx={{ justifyContent: compact ? "center" : "flex-start", minWidth: 0, gap: 1.5, color: "text.secondary" }}>
          {collapsed ? <PanelLeftOpen size={18} /> : <><PanelLeftClose size={18} />Collapse</>}
        </Button>
      </Tooltip>
    </Box>}
  </>;
  return <Box className="studio-shell" sx={{ display: "flex", minHeight: "100vh", "--studio-rail-width": `${width}px` }}>
    <a href="#main-content" className="studio-skip">Skip to content</a>
    <Drawer variant="permanent" sx={{ width, flexShrink: 0, display: { xs: "none", md: "block" }, transition: "width .2s ease",
      "& .MuiDrawer-paper": { width, transition: "width .2s ease", overflowX: "hidden", boxSizing: "border-box", bgcolor: "#0a0c0f" } }}>{navigation(collapsed)}</Drawer>
    <Drawer variant="temporary" open={mobileOpen} onClose={() => setMobileOpen(false)} sx={{ display: { xs: "block", md: "none" }, "& .MuiDrawer-paper": { width: 236 } }}>{navigation(false, true)}</Drawer>
    <Box sx={{ flex: 1, minWidth: 0 }}>
      <Stack component="header" direction="row" className="studio-topbar" sx={{ height: 68, px: { xs: 2, md: 3.5 }, alignItems: "center", gap: 1.5, borderBottom: 1, borderColor: "divider" }}>
        <IconButton aria-label="Open navigation" onClick={() => setMobileOpen(true)} sx={{ display: { md: "none" } }}><Menu size={20} /></IconButton>
        <Typography component="h2" sx={{ flex: 1, fontSize: 15, fontWeight: 550 }}>{items.find(item => item.key === screen)?.label || "Create"}</Typography>
        <Button startIcon={<Plus size={16} />} onClick={onNew} variant="outlined" color="inherit">New project</Button>
      </Stack>
      <Box component="main" id="main-content" tabIndex={-1} sx={{ px: { xs: 2, md: 3.5 }, py: { xs: 3, md: 4 }, maxWidth: 1600, mx: "auto" }}>{children}</Box>
    </Box>
  </Box>;
}
