import { useRef, useState } from "react";

// A separate keyed element resets fallback/timing when a preview is replaced.
export default function ShotPreviewImage({ shot, onOpen }) {
  return <Preview key={shot.still_frame_key || shot.still_frame_url} shot={shot} onOpen={onOpen} />;
}

function Preview({ shot, onOpen }) {
  const [fallback, setFallback] = useState(false);
  const started = useRef(performance.now());
  const display = shot.still_frame_display?.source_key === shot.still_frame_key ? shot.still_frame_display : null;
  const variants = display?.variants?.filter(v => v.url && v.width) || [];
  const candidates = variants.map(v => `${v.url} ${v.width}w`);
  if (display?.width && !variants.some(v => v.width === display.width)) candidates.push(`${shot.still_frame_url} ${display.width}w`);
  const srcSet = !fallback && variants.length ? candidates.join(", ") : undefined;
  const Wrapper = onOpen ? "button" : "a";
  return <Wrapper type={onOpen ? "button" : undefined} className={onOpen ? "preview-enlarge" : undefined} onClick={onOpen} href={onOpen ? undefined : shot.still_frame_url} target="_blank" rel="noreferrer" aria-label={`${onOpen ? "Enlarge" : "Open full-resolution"} preview for shot ${shot.shot_number}`}>
    <img src={shot.still_frame_url} srcSet={srcSet} sizes="(max-width: 600px) calc(100vw - 64px), 600px"
      width={display?.width} height={display?.height} decoding="async" loading="lazy"
      alt={`Shot ${shot.shot_number} opening frame — ${shot.description}`}
      className="w-full rounded-md preview-reveal" data-testid={`shot-preview-${shot.shot_number}`}
      onError={() => { if (!fallback) setFallback(true); }}
      onLoad={event => {
        // Local, inspectable browser measurement; no new telemetry endpoint or
        // signed URLs/user content sent to shared logs. Includes lazy-load wait.
        event.currentTarget.dataset.displayElapsedMs = String(Math.round(performance.now() - started.current));
        event.currentTarget.dataset.displayFormat = event.currentTarget.currentSrc.includes(".webp") ? "webp" : "original";
        const resource = performance.getEntriesByName(event.currentTarget.currentSrc).at(-1);
        if (resource) {
          event.currentTarget.dataset.resourceDurationMs = String(Math.round(resource.duration));
          // Cross-origin S3 may hide transfer size without Timing-Allow-Origin.
          event.currentTarget.dataset.transferBytes = resource.transferSize ? String(resource.transferSize) : "unavailable-or-cached";
        }
      }} />
  </Wrapper>;
}
