import { useEffect, useState } from "react";
import { Alert, Box, Button, Card, CardActions, CardContent, Chip, Skeleton, Stack, Typography } from "@mui/material";
import { friendlyMessage } from "../utils/presentation";
import { listJobs } from "../api/client";

export default function JobHistory({ onResume, onNew }) {
  const [jobs, setJobs] = useState([]);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [more, setMore] = useState(false);
  async function load(offset = 0) {
    setBusy(true); setError("");
    try {
      const data = await listJobs(offset);
      setJobs((current) => offset ? [...current, ...data.jobs] : data.jobs);
      setMore(data.has_more);
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }
  useEffect(() => { load(); }, []);
  return <Box component="section" sx={{ maxWidth: 1200, mx: "auto" }}>
    <Stack direction="row" sx={{ mb: 2, justifyContent: "space-between" }}>
      <Box><Typography variant="h1">Job history</Typography><Typography color="text.secondary" sx={{ mt: 1 }}>Pick up where you left off.</Typography></Box>

    </Stack>
    {error && <Alert severity="error" action={<Button color="inherit" onClick={() => load(jobs.length)}>Retry</Button>}>{friendlyMessage(error, "Your job history could not be loaded. Please retry.")}</Alert>}
    <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", lg: "repeat(2, minmax(0, 1fr))" }, gap: 3, mt: 3 }}>
      {jobs.map((job) => <Card key={job.id} variant="outlined">
        <CardContent>
          <Chip size="small" label={{done: "Ready to open", error: "Needs attention", running: "In progress", queued: "Waiting to start"}[job.status] || "In progress"} sx={{ mb: 2 }} />
          <Typography component="h2">{job.brief}</Typography>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 2 }}>{new Date(job.created_at).toLocaleString()}</Typography>
        </CardContent>
        <CardActions><Button onClick={() => onResume(job.id)}>Resume job</Button></CardActions>
      </Card>)}
      {busy && <Skeleton variant="rounded" height={120} aria-label="Loading job history" />}
      {!busy && !error && !jobs.length && <Typography>No jobs yet. Create a job to get started.</Typography>}
    </Box>
    {more && <Button disabled={busy} onClick={() => load(jobs.length)} sx={{ mt: 2 }}>Load older jobs</Button>}
  </Box>;
}
