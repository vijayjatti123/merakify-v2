const BASE_URL = import.meta.env.VITE_BACKEND_URL || "http://localhost:8000";

export async function createJob(payload) {
  const res = await fetch(`${BASE_URL}/api/jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error("Failed to create job");
  return res.json();
}

export async function listAssets() {
  const res = await fetch(`${BASE_URL}/api/assets`);
  if (!res.ok) throw new Error("Failed to load assets");
  return res.json();
}

export async function uploadAsset(file) {
  const body = new FormData();
  body.append("file", file);
  const res = await fetch(`${BASE_URL}/api/assets/upload`, {
    method: "POST",
    body,
  });
  if (!res.ok) throw new Error("Failed to upload asset");
  return res.json();
}

export async function listCharacters() {
  const res = await fetch(`${BASE_URL}/api/characters`);
  if (!res.ok) throw new Error("Failed to load the Character Vault");
  return res.json();
}

export async function generateCharacter({ name, description, characterId }) {
  const res = await fetch(`${BASE_URL}/api/characters/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, description, character_id: characterId || null }),
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

export async function regenerateShot(jobId, shotNumber) {
  const res = await fetch(`${BASE_URL}/api/jobs/${jobId}/shots/${shotNumber}/regenerate`, {
    method: "POST",
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
