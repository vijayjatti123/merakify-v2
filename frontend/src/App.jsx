import { useState } from "react";
import { createJob } from "./api/client";
import CharacterVault from "./pages/CharacterVault";
import JobView from "./pages/JobView";
import NewJob from "./pages/NewJob";

export default function App() {
  const [jobId, setJobId] = useState(null);
  const [submittedBrief, setSubmittedBrief] = useState("");
  const [screen, setScreen] = useState("director");

  async function handleSubmit(payload) {
    const job = await createJob(payload);
    setSubmittedBrief(payload.brief.split("\n\n")[0]);
    setJobId(job.id);
  }

  function handleReset() {
    setJobId(null);
    setSubmittedBrief("");
  }

  if (screen === "vault") {
    return <CharacterVault onBack={() => setScreen("director")} />;
  }

  return (
    <>
      {!jobId && (
        <nav className="app-view-switcher" aria-label="Workspace views">
          <button type="button" data-active="true">Director</button>
          <button type="button" onClick={() => setScreen("vault")}>Character Vault</button>
        </nav>
      )}
      <main className={`phase-one-shell ${jobId ? "phase-one-shell--active" : ""}`}>
        <NewJob onSubmit={handleSubmit} collapsed={Boolean(jobId)} submittedBrief={submittedBrief} />
        {jobId && <JobView key={jobId} jobId={jobId} onReset={handleReset} />}
      </main>
    </>
  );
}
