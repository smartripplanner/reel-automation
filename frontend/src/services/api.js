import axios from "axios";

// In production (Render/Vercel), set REACT_APP_API_URL in the hosting env.
// In local dev, it falls back to the Vite dev server proxy target.
const API_BASE = process.env.REACT_APP_API_URL || "http://127.0.0.1:8000";

const api = axios.create({
  baseURL: API_BASE,
});

export const getAutomationStatus = async () => {
  const { data } = await api.get("/automation/status");
  return data;
};

export const startAutomation = async () => {
  const { data } = await api.post("/automation/start");
  return data;
};

export const stopAutomation = async () => {
  const { data } = await api.post("/automation/stop");
  return data;
};

export const generateReel = async () => {
  const { data } = await api.post("/automation/generate");
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

// ── Async reel generation (job-based) ────────────────────────────────────────

export const generateReelJob = async () => {
  const { data } = await api.post("/automation/generate-async");
  return data; // { job_id, status, message }
};

export const getJobStatus = async (jobId) => {
  const { data } = await api.get(`/jobs/${jobId}`);
  return data; // { id, status, logs, result }
};
