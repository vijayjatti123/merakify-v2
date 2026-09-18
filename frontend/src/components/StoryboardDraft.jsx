import { Box, Card, Chip, Stack, Typography } from '@mui/material';

export default function StoryboardDraft({ draft, stopped }) {
  return <Box data-testid="storyboard-draft" sx={{ p: { xs: 2, md: 3 } }}>
    <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 2 }}>
      <Chip size="small" color={stopped ? 'warning' : 'primary'} label={stopped ? 'Draft · review paused' : 'Draft · checking continuity'} />
    </Stack>
    <Typography variant="h6" sx={{ mb: 1 }}>{draft.logline}</Typography>
    <Typography color="text.secondary" variant="body2" sx={{ mb: 3 }}>
      {stopped ? 'This draft is saved, but has not passed review. Retry planning to continue.' : 'Explore the sequence while we check it. Shots may change during review; editing and generation unlock afterward.'}
    </Typography>
    <Box className="draft-storyboard-grid">
      {draft.shots.map((shot, index) => <Card component="article" key={shot.shot_number} sx={{ p: 3, borderRadius: 4 }}>
        <Typography color="primary" variant="overline">{index === 0 ? 'Opening' : index === draft.shots.length - 1 ? 'Ending' : 'Main moment'} · Shot {shot.shot_number}</Typography>
        <Typography sx={{ mt: 1, mb: 2 }}>{shot.description}</Typography>
        {shot.dialogue_text && <Typography variant="body2" sx={{ fontStyle: 'italic', mb: 2 }}>“{shot.dialogue_text}”</Typography>}
        <Typography variant="caption" color="text.secondary">Planned {shot.duration_sec}s · Scene {shot.scene_number}</Typography>
      </Card>)}
    </Box>
  </Box>;
}
