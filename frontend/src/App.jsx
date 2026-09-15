import { useEffect, useState } from "react";
import { Alert, Box, Button } from "@mui/material";
import { createJob, getJob } from "./api/client";
import JobHistory from "./components/JobHistory";
import VersionNotice from "./components/VersionNotice";
import ActionProgress from "./components/ActionProgress";
import DashboardLayout from "./components/DashboardLayout";
import CharacterVault from "./pages/CharacterVault";
import JobView from "./pages/JobView";
import NewJob from "./pages/NewJob";

export default function App() {
  const [jobId, setJobId] = useState(() => new URLSearchParams(window.location.search).get("job"));
  const [job, setJob] = useState(null);
  const [lookupError, setLookupError] = useState(false);
  const [lookupAttempt, setLookupAttempt] = useState(0);
  const [screen, setScreen] = useState("director");

  useEffect(() => {
    const restore = () => { setJobId(new URLSearchParams(window.location.search).get("job")); setScreen("director"); };
    window.addEventListener("popstate", restore);
    return () => window.removeEventListener("popstate", restore);
  }, []);

  useEffect(() => {
    let cancelled = false;
    setJob(null); setLookupError(false);
    if (jobId) getJob(jobId).then((value) => {
      if (!cancelled) setJob(value);
    }).catch(() => { if (!cancelled) setLookupError(true); });
    return () => { cancelled = true; };
  }, [jobId, lookupAttempt]);

  function openJob(id) {
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("job", id);
    else url.searchParams.delete("job");
    window.history.pushState(null, "", url);
    setJobId(id); setScreen("director");
  }

  async function handleSubmit(payload) {
    const created = await createJob(payload);
    openJob(created.id);
  }


  return (
    <>
      <VersionNotice />
      <DashboardLayout screen={screen} onNavigate={setScreen} onNew={() => openJob(null)}>
      {screen === "vault" ? <CharacterVault onBack={() => setScreen("director")} /> :
       screen === "history" ? <JobHistory onResume={openJob} onNew={() => openJob(null)} /> :
      <div className={`phase-one-shell ${jobId ? "phase-one-shell--active" : ""}`}>
        <NewJob onSubmit={handleSubmit} collapsed={Boolean(jobId)} submittedBrief={job?.brief.split("\n\n")[0] || ""} />
        {jobId && !job && <Box sx={{ p: 3 }}>
          {lookupError ? <Alert severity="error" action={<Button color="inherit" onClick={() => setLookupAttempt((n) => n + 1)}>Retry</Button>}>
            Could not open this job. It may be unavailable, or the connection may have failed. You can also choose another job from Job history.
          </Alert> : <ActionProgress label="Loading your job…" />}
        </Box>}
        {jobId && job?.id === jobId && <JobView key={jobId} jobId={jobId} initialJob={job} onReset={() => openJob(null)} onRetry={(retried) => openJob(retried.id)} />}
      </div>}
      </DashboardLayout>
    </>
  );
}
