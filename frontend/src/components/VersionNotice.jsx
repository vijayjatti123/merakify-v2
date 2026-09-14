import { useEffect, useState } from "react";
import { Alert, Button, Snackbar } from "@mui/material";

export default function VersionNotice() {
  const [outdated, setOutdated] = useState(false);
  useEffect(() => {
    let cancelled = false;
    let checking = false;
    async function check() {
      if (checking) return;
      checking = true;
      try {
        const response = await fetch(`/build-version.json?t=${Date.now()}`, { cache: "no-store" });
        if (!response.ok) return;
        const value = await response.json();
        if (!cancelled && typeof value.version === "string" && value.version !== __BUILD_VERSION__) setOutdated(true);
      } catch { /* Offline/older servers must not produce a false update alert. */ }
      finally { checking = false; }
    }
    check();
    const timer = setInterval(check, 60000);
    window.addEventListener("focus", check);
    return () => { cancelled = true; clearInterval(timer); window.removeEventListener("focus", check); };
  }, []);
  return <Snackbar open={outdated} anchorOrigin={{ vertical: "bottom", horizontal: "center" }}>
    <Alert severity="info" variant="filled" action={<Button color="inherit" onClick={() => window.location.reload()}>Refresh</Button>}>
      New version available — refresh. Save any unsent changes first.
    </Alert>
  </Snackbar>;
}
