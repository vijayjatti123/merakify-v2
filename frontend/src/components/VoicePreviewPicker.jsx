import { useEffect, useRef, useState } from 'react';
import { Alert, Box, Button, CircularProgress, Dialog, DialogContent, DialogTitle, IconButton, List, ListItem, ListItemButton, ListItemText, MenuItem, Stack, TextField, Typography } from '@mui/material';
import { Check, ChevronDown, Pause, Play, X } from 'lucide-react';
import { SARVAM_VOICES } from '../utils/voices';

const BASE_URL = import.meta.env.VITE_BACKEND_URL || 'http://localhost:8000';
const LANGUAGES = ['English', 'Hindi', 'Tamil', 'Telugu', 'Bengali'];
const title = (voice) => voice.charAt(0).toUpperCase() + voice.slice(1);

export default function VoicePreviewPicker({ value, onChange, disabled = false }) {
  const [open, setOpen] = useState(false);
  const [language, setLanguage] = useState('English');
  const [search, setSearch] = useState('');
  const [catalog, setCatalog] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [playing, setPlaying] = useState(null);
  const [buffering, setBuffering] = useState(null);
  const audio = useRef(null);
  const playback = useRef(0);
  const stop = () => { playback.current += 1; audio.current?.pause(); setPlaying(null); setBuffering(null); };

  useEffect(() => {
    stop();
    if (!open) return;
    const controller = new AbortController();
    setLoading(true); setCatalog(null); setError('');
    fetch(`${BASE_URL}/api/voice-previews?language=${encodeURIComponent(language)}`, { signal: controller.signal, cache: 'no-store' })
      .then(async (response) => {
        if (!response.ok) throw new Error('Previews are unavailable right now. You can still choose and save a voice.');
        return response.json();
      })
      .then((data) => { if (!controller.signal.aborted) setCatalog({ ...data, loadedAt: Date.now() }); })
      .catch((err) => { if (!controller.signal.aborted) setError(err.message); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [open, language]);

  useEffect(() => () => { audio.current?.pause(); }, []);

  async function preview(voice) {
    if (playing === voice) { stop(); return; }
    stop();
    const attempt = playback.current;
    setBuffering(voice); setError('');
    try {
      let current = catalog;
      if (!current || Date.now() - current.loadedAt > 25 * 60 * 1000) {
        const response = await fetch(`${BASE_URL}/api/voice-previews?language=${encodeURIComponent(language)}`, { cache: 'no-store' });
        if (!response.ok) throw new Error('Preview unavailable. Close and reopen the voice picker to retry.');
        current = { ...await response.json(), loadedAt: Date.now() };
        if (attempt !== playback.current) return;
        setCatalog(current);
      }
      const sample = current.voices.find((entry) => entry.voice_id === voice);
      if (!sample?.url) throw new Error('No cached preview for this voice yet. You can still select it.');
      if (attempt !== playback.current || !audio.current) return;
      audio.current.src = sample.url;
      await audio.current.play();
      if (attempt === playback.current) { setPlaying(voice); setBuffering(null); }
    } catch (err) {
      if (attempt === playback.current) { setError(err.message || 'Could not play the preview. Please retry.'); setBuffering(null); }
    }
  }

  return <Box>
    <Typography variant="body2" sx={{ mb: 1 }}>Character voice</Typography>
    <Button type="button" variant="outlined" fullWidth disabled={disabled} endIcon={<ChevronDown size={18} />} onClick={() => setOpen(true)} data-testid="voice-picker-open">
      {value ? title(value) : 'Choose a voice'}
    </Button>
    <Dialog open={open} onClose={() => setOpen(false)} fullWidth maxWidth="xs" aria-labelledby="voice-picker-title">
      <DialogTitle id="voice-picker-title">Choose a voice
        <IconButton aria-label="Close voice picker" onClick={() => setOpen(false)} sx={{ position: 'absolute', right: 12, top: 12 }}><X size={20} /></IconButton>
      </DialogTitle>
      <DialogContent>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>Listen first, then choose. Playing a sample won't change your selection.</Typography>
        <Stack spacing={2}>
          <TextField select label="Preview language" value={language} onChange={(e) => setLanguage(e.target.value)} helperText="Preview only — this does not change your character or video language.">
            {LANGUAGES.map((name) => <MenuItem value={name} key={name}>{name}</MenuItem>)}
          </TextField>
          <TextField label="Find a voice" value={search} onChange={(e) => setSearch(e.target.value)} />
          {error && <Alert severity="warning">{error}</Alert>}
          {loading && <Typography role="status" variant="body2">Loading cached previews…</Typography>}
        </Stack>
        <List sx={{ maxHeight: 320, overflowY: 'auto', mt: 1 }} aria-label="Available voices">
          {SARVAM_VOICES.filter((voice) => voice.includes(search.trim().toLowerCase())).map((voice) => {
            const available = catalog?.voices.some((entry) => entry.voice_id === voice && entry.available);
            return <ListItem key={voice} disablePadding secondaryAction={<IconButton type="button" aria-label={`${playing === voice ? 'Pause' : 'Preview'} ${title(voice)} in ${language}`} disabled={loading || !available} onClick={() => preview(voice)} data-testid={`voice-preview-${voice}`}>
              {buffering === voice ? <CircularProgress size={18} /> : playing === voice ? <Pause size={18} /> : <Play size={18} />}
            </IconButton>}>
              <ListItemButton selected={value === voice} onClick={() => { onChange(voice); setOpen(false); }} data-testid={`voice-select-${voice}`}>
                <ListItemText primary={title(voice)} secondary={!loading && !available ? 'Preview not available yet' : undefined} />
                {value === voice && <Check size={16} aria-label="Selected" />}
              </ListItemButton>
            </ListItem>;
          })}
        </List>
        <Typography role="status" variant="caption" color="text.secondary">{playing ? `Playing ${title(playing)} · ${language}` : `Selected: ${value ? title(value) : 'none'}`} · Cached sample, no generation charge</Typography>
        <audio ref={audio} preload="none" data-testid="voice-preview-audio" onEnded={() => { setPlaying(null); setBuffering(null); }} onError={() => { setPlaying(null); setBuffering(null); setCatalog(null); setError('Could not load the sample. Close and reopen the picker to retry. Your voice choice is unchanged.'); }} />
      </DialogContent>
    </Dialog>
  </Box>;
}
