import axios from "axios";

// Vite exposes env vars via import.meta.env (not process.env).
// Set VITE_API_URL in frontend/.env (local) or the hosting dashboard (prod).
const API_BASE = import.meta.env.VITE_API_URL || "http://127.0.0.1:8000";

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

// Trigger a single reel via GitHub Actions.
// Returns { job_id, status } from the in-memory job tracker.
export const generateReel = async (topic = "Travel Tips & Hidden Gems") => {
  const { data } = await api.post("/automation/generate", { topic });
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

// ── Async reel generation (job-based, DB-backed) ─────────────────────────────

export const generateReelJob = async () => {
  const { data } = await api.post("/automation/generate-async");
  return data; // { job_id, status, message }
};

export const getJobStatus = async (jobId) => {
  const { data } = await api.get(`/jobs/${jobId}`);
  return data; // { id, status, logs, result }
};
