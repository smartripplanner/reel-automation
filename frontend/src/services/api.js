import axios from "axios";

// Vite exposes env vars via import.meta.env (not process.env).
// Set VITE_API_URL in frontend/.env or leave blank to use localhost:8000.
const API_BASE = import.meta.env.VITE_API_URL || "http://127.0.0.1:8000";

const api = axios.create({
  baseURL: API_BASE,
});

export const getAutomationStatus = async () => {
  const { data } = await api.get("/automation/status");
  return data;
};

// Returns { job_id, status } — backend queues the pipeline and returns immediately
export const startAutomation = async () => {
  const { data } = await api.post("/automation/start");
  return data;
};

export const stopAutomation = async () => {
  const { data } = await api.post("/automation/stop");
  return data;
};

/**
 * Trigger a single reel with an optional explicit topic.
 * If topic is empty/undefined the backend uses the saved niche from Settings.
 * Returns { job_id, status, topic }
 */
export const generateReel = async (topic) => {
  const payload = { topic: topic?.trim() || "" };
  const { data } = await api.post("/automation/generate", payload);
  return data;
};

export const generateBatchReels = async (count) => {
  const { data } = await api.post("/automation/generate-batch", { count });
  return data;
};

export const getReels = async () => {
  const { data } = await api.get("/reels");
  return data;
};

export const getLogs = async () => {
  const { data } = await api.get("/logs");
  return data;
};

export const getSettings = async () => {
  const { data } = await api.get("/settings");
  return data;
};

export const updateSettings = async (payload) => {
  const { data } = await api.post("/settings", payload);
  return data;
};

// Poll a running job for live logs and final status
export const getJobStatus = async (jobId) => {
  const { data } = await api.get(`/jobs/${jobId}`);
  return data; // { id, status, logs, result }
};

/**
 * Returns the URL for in-browser <video> streaming of a completed reel.
 * Use this as the `src` of a <video> element.
 */
export const getVideoUrl = (jobId) => `${API_BASE}/jobs/${jobId}/video`;

/**
 * Returns the download URL for a completed reel.
 * Use this as the `href` of an <a download> element.
 */
export const getDownloadUrl = (jobId) => `${API_BASE}/jobs/${jobId}/download`;
