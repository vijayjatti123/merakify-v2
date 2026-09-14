import { useEffect, useState } from "react";
import { Alert, Box, Button, Card, CardActions, CardContent, Chip, Skeleton, Stack, Typography } from "@mui/material";
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
  return <Box component="main" sx={{ maxWidth: 1000, mx: "auto", p: 3 }}>
    <Stack direction="row" justifyContent="space-between" sx={{ mb: 2 }}>
      <Typography variant="h5" component="h1">Job history</Typography>
      <Button variant="outlined" onClick={onNew}>New job</Button>
    </Stack>
    {error && <Alert severity="error" action={<Button color="inherit" onClick={() => load(jobs.length)}>Retry</Button>}>{error}</Alert>}
    <Stack spacing={2}>
      {jobs.map((job) => <Card key={job.id} variant="outlined">
        <CardContent>
          <Chip size="small" label={job.status} sx={{ mb: 1 }} />
          <Typography component="h2">{job.brief}</Typography>
          <Typography variant="caption" color="text.secondary">{new Date(job.created_at).toLocaleString()} · {job.id}</Typography>
        </CardContent>
        <CardActions><Button onClick={() => onResume(job.id)}>Resume job</Button></CardActions>
      </Card>)}
      {busy && <Skeleton variant="rounded" height={120} aria-label="Loading job history" />}
      {!busy && !error && !jobs.length && <Typography>No jobs yet. Create a job to get started.</Typography>}
    </Stack>
    {more && <Button disabled={busy} onClick={() => load(jobs.length)} sx={{ mt: 2 }}>Load older jobs</Button>}
  </Box>;
}
