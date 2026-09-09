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
