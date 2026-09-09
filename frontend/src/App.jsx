import { useState } from "react";
import { createJob } from "./api/client";
import JobView from "./pages/JobView";
import NewJob from "./pages/NewJob";

export default function App() {
  const [jobId, setJobId] = useState(null);

  async function handleSubmit(payload) {
    const job = await createJob(payload);
    setJobId(job.id);
  }

  if (jobId) {
    return <JobView key={jobId} jobId={jobId} onReset={() => setJobId(null)} onRetry={setJobId} />;
  }
  return <NewJob onSubmit={handleSubmit} />;
}
