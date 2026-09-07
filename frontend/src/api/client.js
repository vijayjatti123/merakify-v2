const BASE_URL = import.meta.env.VITE_BACKEND_URL || "http://localhost:8000";

export async function createJob(brief) {
  const res = await fetch(`${BASE_URL}/api/jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ brief }),
  });
  if (!res.ok) throw new Error("Failed to create job");
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
