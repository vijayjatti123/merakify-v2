const BASE_URL = import.meta.env.VITE_BACKEND_URL || "http://localhost:8000";

export async function replaceShotPreview(jobId, number, action, payload) {
  const multipart = payload instanceof FormData;
  const res = await fetch(`${BASE_URL}/api/jobs/${jobId}/shots/${number}/preview/${action}`, {
    method: "POST", ...(multipart ? {} : { headers: { "Content-Type": "application/json" } }),
    body: multipart ? payload : JSON.stringify(payload),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : "Couldn't update this image. Please try again.");
  }
  return res.json();
}

export async function listJobs(offset = 0) {
  const res = await fetch(`${BASE_URL}/api/jobs?limit=30&offset=${offset}`, { cache: "no-store" });
  if (!res.ok) throw new Error("Could not load job history. Please try again.");
  return res.json();
}

export async function retryFailedJob(jobId) {
  const res = await fetch(`${BASE_URL}/api/jobs/${jobId}/retry-failed`, { method: "POST" });
  if (!res.ok) throw new Error("Could not restart planning. Please try again.");
  return res.json();
}

export async function getJob(jobId) {
  const res = await fetch(`${BASE_URL}/api/jobs/${jobId}`);
  if (!res.ok) throw new Error("Could not refresh video status; retrying shortly.");
  return res.json();
}

export async function generateShotVideo(jobId, shotNumber) {
  const res = await fetch(`${BASE_URL}/api/jobs/${jobId}/shots/${shotNumber}/video`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}) });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || "Video submission uncertain; refresh the shot status.");
  }
  return res.json();
}

export async function createJob(payload) {
  const res = await fetch(`${BASE_URL}/api/jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const mentionError = typeof data.detail === "string" && /^(A selected character|Two selections|Invalid character mention)/.test(data.detail);
    throw new Error(mentionError ? `Please review your character selection. ${data.detail}` : "Failed to create job");
  }
  return res.json();
}

export async function extractScript(scriptText) {
  const res = await fetch(`${BASE_URL}/api/scripts/extract`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ script_text: scriptText }),
  });
  if (!res.ok) {
    const payload = await res.json().catch(() => ({}));
    throw new Error(payload.detail || "Failed to extract script references");
  }
  return res.json();
}

export async function listAssets() {
  const res = await fetch(`${BASE_URL}/api/assets`);
  if (!res.ok) throw new Error("Failed to load assets");
  return res.json();
}

export async function uploadAsset(file, { role = "", label = "" } = {}) {
  const body = new FormData();
  body.append("file", file);
  if (role) body.append("role", role);
  if (label.trim()) body.append("label", label.trim());
  const res = await fetch(`${BASE_URL}/api/assets/upload`, {
    method: "POST",
    body,
  });
  if (!res.ok) throw new Error("Failed to upload asset");
  return res.json();
}

export async function listCharacters() {
  const res = await fetch(`${BASE_URL}/api/characters`, { cache: "no-store" });
  if (!res.ok) throw new Error("Failed to load the Character Vault");
  return res.json();
}

export async function generateCharacter({ name, description, characterId }) {
  const res = await fetch(`${BASE_URL}/api/characters/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, display_name: name.trim(), catalog_status: "customer", description, character_id: characterId || null }),
  });
  if (!res.ok) {
    const payload = await res.json().catch(() => ({}));
    throw new Error(payload.detail || "Failed to generate character image");
  }
  return res.json();
}

export async function uploadCharacter({ name, description, file, characterId }) {
  const body = new FormData();
  body.append("name", name);
  body.append("display_name", name.trim());
  body.append("catalog_status", "customer");
  body.append("description", description);
  body.append("file", file);
  if (characterId) body.append("character_id", characterId);
  const res = await fetch(`${BASE_URL}/api/characters/upload`, { method: "POST", body });
  if (!res.ok) {
    const payload = await res.json().catch(() => ({}));
    throw new Error(payload.detail || "Failed to upload character image");
  }
  return res.json();
}

export async function setCharacterVoice(characterId, voiceId) {
  const res = await fetch(`${BASE_URL}/api/characters/${characterId}/voice`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ voice_id: voiceId }),
  });
  if (!res.ok) {
    const payload = await res.json().catch(() => ({}));
    throw new Error(payload.detail || "Failed to save character voice");
  }
  return res.json();
}

export async function approveCharacter(characterId) {
  const res = await fetch(`${BASE_URL}/api/characters/${characterId}/approve`, { method: "POST" });
  if (!res.ok) {
    const payload = await res.json().catch(() => ({}));
    throw new Error(payload.detail || "Failed to approve character");
  }
  return res.json();
}

export async function retryJob(jobId, changeRequest) {
  const res = await fetch(`${BASE_URL}/api/jobs/${jobId}/retry`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ change_request: changeRequest.trim() || null }),
  });
  if (!res.ok) throw new Error("Failed to retry job");
  return res.json();
}

export async function reviseJob(jobId, shots) {
  const res = await fetch(`${BASE_URL}/api/jobs/${jobId}/revise`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ shots }),
  });
  if (!res.ok) throw new Error("Failed to revise job");
  return res.json();
}

export async function approveJob(jobId) {
  const res = await fetch(`${BASE_URL}/api/jobs/${jobId}/approve`, {
    method: "POST",
  });
  if (!res.ok) throw new Error("Failed to approve shot list");
  return res.json();
}

export async function regenerateShot(jobId, shotNumber, hints = {}) {
  const selectedHints = Object.fromEntries(Object.entries(hints).filter(([, value]) => value));
  const res = await fetch(`${BASE_URL}/api/jobs/${jobId}/shots/${shotNumber}/regenerate`, {
    method: "POST",
    ...(Object.keys(selectedHints).length ? {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(selectedHints),
    } : {}),
  });
  if (!res.ok) throw new Error("Failed to regenerate shot");
  return res.json();
}

// Returns an EventSource-like object; caller attaches onEvent/onFinal/onError
// and calls close() when done.
export function streamJob(jobId, { onEvent, onFinal, onError }) {
  const source = new EventSource(`${BASE_URL}/api/jobs/${jobId}/stream`);

  source.onmessage = (msg) => {
    try {
      onEvent(JSON.parse(msg.data));
    } catch (e) {
      onError?.(e);
    }
  };

  source.addEventListener("final", (msg) => {
    try {
      onFinal(JSON.parse(msg.data));
    } finally {
      source.close();
    }
  });

  source.addEventListener("error", (msg) => {
    onError?.(msg);
  });

  return source;
}

export async function regenerateShotVideo(jobId, shot, hint = "") {
  const res = await fetch(`${BASE_URL}/api/jobs/${jobId}/shots/${shot.shot_number}/video/regenerate`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ hint, expected_attempt: shot.video_task_id || shot.video_submitted_at || "none" }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "Failed to regenerate video");
  return data;
}

export async function retryShotPreview(jobId, shot) {
  const res = await fetch(`${BASE_URL}/api/jobs/${jobId}/shots/${shot.shot_number}/preview/retry`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ expected_attempt: shot.video_task_id || shot.video_submitted_at || "none" }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "Could not retry this preview. Please try again.");
  return data;
}

export async function retryPreviewPreparation(jobId) {
  const res = await fetch(`${BASE_URL}/api/jobs/${jobId}/previews/retry`, { method: "POST" });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "Could not retry preview preparation.");
  return data;
}


export async function assembleFinalVideo(jobId) {
  const res = await fetch(`${BASE_URL}/api/jobs/${jobId}/assemble`, { method: "POST" });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "Failed to assemble final video");
  return data;
}

export async function enhanceShotFace(jobId, shot) {
  const res = await fetch(`${BASE_URL}/api/jobs/${jobId}/shots/${shot.shot_number}/enhance-face`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ expected_video_key: shot.video_key }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "Could not start face enhancement");
  return data;
}
