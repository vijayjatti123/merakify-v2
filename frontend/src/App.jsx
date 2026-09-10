import { useState } from "react";
import { createJob } from "./api/client";
import JobView from "./pages/JobView";
import NewJob from "./pages/NewJob";

export default function App() {
  const [jobId, setJobId] = useState(null);
  const [submittedBrief, setSubmittedBrief] = useState("");

  async function handleSubmit(payload) {
    const job = await createJob(payload);
    setSubmittedBrief(payload.brief.split("\n\n")[0]);
    setJobId(job.id);
  }

  function handleReset() {
    setJobId(null);
    setSubmittedBrief("");
  }

  return (
    <main className={`phase-one-shell ${jobId ? "phase-one-shell--active" : ""}`}>
      <NewJob onSubmit={handleSubmit} collapsed={Boolean(jobId)} submittedBrief={submittedBrief} />
      {jobId && <JobView key={jobId} jobId={jobId} onReset={handleReset} />}
    </main>
  );
}
