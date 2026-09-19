import { AD_TYPES } from "./CommercialIntake";
import StudioSelect from "./StudioSelect";
import { ArrowUpRight, CalendarDays, Film } from "lucide-react";
import { useEffect, useState } from "react";
import { Alert, Box, Button, Card, CardActions, CardContent, Chip, Skeleton, Stack, TextField, Typography } from "@mui/material";
import { friendlyMessage } from "../utils/presentation";
import { listJobs } from "../api/client";

export default function JobHistory({ onResume, onNew }) {
  const [jobs, setJobs] = useState([]);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("All projects");
  const [sort, setSort] = useState("Newest first");
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
  const visibleJobs = jobs.filter(job =>
    job.brief.toLowerCase().includes(query.trim().toLowerCase()) &&
    (status === "All projects" || (status === "Needs attention" ? job.status === "error" : status === "Ready" ? job.status === "done" : ["running", "queued"].includes(job.status)))
  ).sort((a, b) => (sort === "Newest first" ? -1 : 1) * (new Date(a.created_at) - new Date(b.created_at)));
  return <Box component="section" sx={{ maxWidth: 1200, mx: "auto" }}>
    <Stack direction="row" sx={{ mb: 4, justifyContent: "space-between" }}>
      <Box><p className="creator-eyebrow">YOUR LIBRARY</p><Typography variant="h1">My projects</Typography><Typography color="text.secondary" sx={{ mt: 1 }}>Your stories, drafts and finished work. All in one place.</Typography></Box>

    </Stack>
    <div className="studio-library-toolbar">
      <TextField label="Search projects" value={query} onChange={event => setQuery(event.target.value)} />
      <StudioSelect label="Show" value={status} onChange={event => setStatus(event.target.value)}>
        <option>All projects</option><option>Ready</option><option>In progress</option><option>Needs attention</option>
      </StudioSelect>
      <StudioSelect label="Sort" value={sort} onChange={event => setSort(event.target.value)}>
        <option>Newest first</option><option>Oldest first</option>
      </StudioSelect>
    </div>
    <Typography variant="caption" color="text.secondary" role="status">{visibleJobs.length} of {jobs.length} loaded projects</Typography>
    {error && <Alert severity="error" action={<Button color="inherit" onClick={() => load(jobs.length)}>Retry</Button>}>{friendlyMessage(error, "Your job history could not be loaded. Please retry.")}</Alert>}
    <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", md: "repeat(2, minmax(0, 1fr))", xl: "repeat(3, minmax(0, 1fr))" }, gap: 2, mt: 2 }}>
      {visibleJobs.map((job, index) => <Card key={job.id} className={`history-card history-tone-${index % 3}`}>
        <CardContent>
          <Box className="history-card-top"><span className="creator-icon"><Film size={22} /></span>
          <Chip size="small" label={{done: "Ready to open", error: "Needs attention", running: "In progress", queued: "Waiting to start"}[job.status] || "In progress"} sx={{ mb: 2 }} />
          </Box>
          <Typography variant="caption" color="text.secondary">{AD_TYPES.find(type => type.id === job.ad_type)?.label || "Character Commercial"}</Typography>
          <Typography component="h2" sx={{ fontWeight: 500, lineHeight: 1.65 }}>{job.brief}</Typography>
          <Typography variant="caption" color="text.secondary" sx={{ display: "flex", alignItems: "center", gap: 1, mt: 3 }}><CalendarDays size={15} />{new Date(job.created_at).toLocaleString()}</Typography>
        </CardContent>
        <CardActions><Button variant="text" color="primary" endIcon={<ArrowUpRight size={18} />} onClick={() => onResume(job.id)}>Open project</Button></CardActions>
      </Card>)}
      {busy && <Skeleton variant="rounded" height={120} aria-label="Loading job history" />}
      {!busy && !error && !jobs.length && <Stack spacing={2}><Typography>No projects yet. Start with your first idea.</Typography><Button variant="outlined" onClick={onNew}>Create a project</Button></Stack>}
      {!busy && jobs.length > 0 && !visibleJobs.length && <Typography>No matching projects in the loaded results. Try a different search or load older projects.</Typography>}
    </Box>
    {more && <Button disabled={busy} onClick={() => load(jobs.length)} sx={{ mt: 2 }}>Load older jobs</Button>}
  </Box>;
}
