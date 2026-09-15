const BASE_URL = import.meta.env.VITE_BACKEND_URL || "http://localhost:8000";

export async function clarifierRequest(path, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 65000);
  try {
    const response = await fetch(`${BASE_URL}/api/clarifier${path}`, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal: controller.signal,
    });
    if (!response.ok) {
      const error = new Error(response.status === 409
        ? "This conversation changed. Reload it before continuing."
        : "We couldn't update your idea right now. Please try again, or use your original brief.");
      error.status = response.status;
      throw error;
    }
    return await response.json();
  } finally { clearTimeout(timer); }
}
