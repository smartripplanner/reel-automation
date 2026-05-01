import { useEffect, useRef, useState } from "react";

import Dashboard from "./pages/Dashboard";
import Settings from "./pages/Settings";
import { useJobPoller } from "./hooks/useJobPoller";
import {
  generateBatchReels,
  generateReel,
  getAutomationStatus,
  getLogs,
  getReels,
  getSettings,
  startAutomation,
  stopAutomation,
  updateSettings,
  getVideoUrl,
} from "./services/api";

const initialSettings = {
  niche: "Motivation",
  reel_duration: 30,
  reels_per_day: 3,
};

const tabs = ["dashboard", "settings"];

// ── Pipeline progress stages ──────────────────────────────────────────────────
const PIPELINE_STAGES = [
  { key: "script",    label: "Script",   icon: "✍️",  match: /script|topic|generat/i },
  { key: "voice",     label: "Voice",    icon: "🎙️", match: /voice|tts|audio|elevenlabs|edge-tts/i },
  { key: "clips",     label: "Visuals",  icon: "🎬",  match: /clip|pexels|fetch|download.*mp4/i },
  { key: "captions",  label: "Captions", icon: "💬",  match: /caption|subtitle|ass/i },
  { key: "rendering", label: "Render",   icon: "🎞️", match: /render|ffmpeg|encod|segment|concat/i },
  { key: "done",      label: "Done",     icon: "✅",  match: /reel saved|pipeline complete/i },
];

function detectStage(logs) {
  if (!logs || logs.length === 0) return null;
  const allLogs = logs.join(" ").toLowerCase();
  let current = null;
  for (const stage of PIPELINE_STAGES) {
    if (stage.match.test(allLogs)) current = stage.key;
  }
  return current;
}

// ── Error message normaliser ──────────────────────────────────────────────────
const extractErrorMessage = (err, fallback = "Request failed. Please try again.") => {
  const detail = err?.response?.data?.detail;
  if (!detail) return fallback;
  if (Array.isArray(detail)) return detail.map((e) => e?.msg ?? JSON.stringify(e)).join("; ");
  if (typeof detail === "string") return detail;
  return fallback;
};

// ── Video Player component ────────────────────────────────────────────────────
function ReelPlayer({ jobId, topic }) {
  const API_BASE = import.meta.env.VITE_API_URL || "http://127.0.0.1:8000";
  const videoUrl  = `${API_BASE}/jobs/${jobId}/video`;
  const downloadUrl = `${API_BASE}/jobs/${jobId}/download`;
  const videoRef = useRef(null);

  useEffect(() => {
    if (videoRef.current) {
      videoRef.current.load();
    }
  }, [jobId]);

  return (
    <div className="rounded-[2rem] border border-green-200 bg-white/90 p-6 shadow-panel backdrop-blur">
      <div className="flex items-center justify-between mb-4">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.3em] text-green-600">Reel Ready</p>
          <h2 className="mt-1 text-2xl font-extrabold text-ink truncate max-w-lg">
            {topic || "Your Reel"}
          </h2>
        </div>
        <a
          href={downloadUrl}
          download
          className="rounded-2xl bg-accent px-5 py-2.5 text-sm font-semibold text-white shadow transition hover:-translate-y-0.5 hover:opacity-90"
        >
          ⬇ Download MP4
        </a>
      </div>

      <div className="relative overflow-hidden rounded-2xl bg-black" style={{ aspectRatio: "9/16", maxHeight: "70vh", margin: "0 auto" }}>
        <video
          ref={videoRef}
          src={videoUrl}
          controls
          autoPlay
          playsInline
          className="h-full w-full object-contain"
          style={{ maxHeight: "70vh" }}
        >
          Your browser does not support video playback.
        </video>
      </div>

      <p className="mt-3 text-center text-xs text-slate-400">
        Saved to <code className="rounded bg-slate-100 px-1.5 py-0.5 text-slate-600">output/</code> folder in the project root
      </p>
    </div>
  );
}

// ── Pipeline Progress Bar ─────────────────────────────────────────────────────
function PipelineProgress({ currentStage, logs }) {
  const activeIdx = PIPELINE_STAGES.findIndex((s) => s.key === currentStage);
  const lastLog = logs?.[logs.length - 1] ?? "";

  return (
    <div className="rounded-[2rem] border border-blue-200 bg-blue-50/80 p-5 backdrop-blur">
      <div className="flex items-center justify-between mb-3">
        <p className="text-sm font-semibold text-blue-800">Generating your reel...</p>
        <span className="text-xs text-blue-500 font-medium">{lastLog.slice(0, 80)}</span>
      </div>

      {/* Stage pills */}
      <div className="flex items-center gap-1 flex-wrap">
        {PIPELINE_STAGES.map((stage, idx) => {
          const isActive  = idx === activeIdx;
          const isDone    = idx < activeIdx;
          return (
            <div
              key={stage.key}
              className={`flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-semibold transition-all ${
                isDone
                  ? "bg-green-100 text-green-700"
                  : isActive
                  ? "bg-blue-600 text-white shadow-md"
                  : "bg-slate-100 text-slate-400"
              }`}
            >
              <span>{stage.icon}</span>
              <span>{stage.label}</span>
              {isActive && (
                <span className="inline-block h-2 w-2 rounded-full bg-white animate-pulse" />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}


// ── Main App ──────────────────────────────────────────────────────────────────
function App() {
  const [activeTab, setActiveTab]         = useState("dashboard");
  const [status, setStatus]               = useState(null);
  const [reels, setReels]                 = useState([]);
  const [logs, setLogs]                   = useState([]);
  const [settings, setSettings]           = useState(initialSettings);
  const [busy, setBusy]                   = useState(false);
  const [error, setError]                 = useState("");
  const [batchProgress, setBatchProgress] = useState("");
  const [topic, setTopic]                 = useState("");
  const [settingsSavedMsg, setSettingsSavedMsg] = useState("");

  const { jobId, jobStatus, jobLogs, jobResult, startJob, clearJob } = useJobPoller();

  // Derived state
  const currentStage   = detectStage(jobLogs);
  const isJobRunning   = jobStatus === "queued" || jobStatus === "running";
  const jobCompleted   = jobStatus === "completed";
  const jobFailed      = jobStatus === "failed";
  const completedTopic = jobResult?.topic || topic;

  const refreshData = async () => {
    const [statusData, reelsData, logsData, settingsData] = await Promise.all([
      getAutomationStatus(),
      getReels(),
      getLogs(),
      getSettings(),
    ]);
    setStatus(statusData);
    setReels(reelsData);
    setLogs(logsData);
    setSettings(settingsData);
  };

  useEffect(() => {
    refreshData().catch(() => {
      setError("Cannot reach backend. Make sure the FastAPI server is running on port 8000.");
    });
  }, []);

  // When job finishes, refresh data
  useEffect(() => {
    if (jobCompleted) {
      setBusy(false);
      refreshData().catch(() => {});
    } else if (jobFailed) {
      setBusy(false);
    }
  }, [jobStatus]);

  // ── Kick off a single reel job ────────────────────────────────────────────
  const _startReelJob = async (apiFn) => {
    setBusy(true);
    setError("");
    clearJob();

    try {
      const data = await apiFn();
      const jobId = data.job_id;
      if (!jobId) {
        setBusy(false);
        return;
      }
      startJob(jobId);
      // busy stays true until jobStatus effect clears it
    } catch (requestError) {
      setError(extractErrorMessage(requestError, "Failed to start reel generation."));
      setBusy(false);
    }
  };

  const handleStart    = () => _startReelJob(startAutomation);
  const handleGenerate = () => _startReelJob(() => generateReel(topic || undefined));

  const runBatchGeneration = async (count) => {
    setBusy(true);
    setError("");
    setBatchProgress(`Generating ${count} reels...`);
    let intervalId = null;

    try {
      intervalId = window.setInterval(async () => {
        try {
          const latestLogs = await getLogs();
          const prog = latestLogs.find((l) =>
            /Starting reel \d+ of \d+|Finished reel \d+ of \d+/.test(l.message)
          );
          if (prog) setBatchProgress(prog.message);
        } catch { /* keep existing message */ }
      }, 2000);

      await generateBatchReels(count);
      setBatchProgress(`Finished generating ${count} reels.`);
      await refreshData();
      window.setTimeout(() => setBatchProgress(""), 3000);
    } catch (requestError) {
      setError(extractErrorMessage(requestError, "Batch generation failed."));
      setBatchProgress("");
    } finally {
      if (intervalId) window.clearInterval(intervalId);
      setBusy(false);
    }
  };

  const runAction = async (action) => {
    setBusy(true);
    setError("");
    try {
      await action();
      await refreshData();
    } catch (requestError) {
      setError(extractErrorMessage(requestError));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-screen bg-[radial-gradient(circle_at_top_left,_rgba(249,115,22,0.16),_transparent_34%),linear-gradient(180deg,_#f8fafc_0%,_#eff6ff_100%)]">
      <div className="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">

        {/* Header */}
        <header className="rounded-[2rem] border border-white/60 bg-white/75 p-6 shadow-panel backdrop-blur">
          <div className="flex flex-col gap-6 lg:flex-row lg:items-end lg:justify-between">
            <div>
              <p className="text-sm font-semibold uppercase tracking-[0.3em] text-accent">Local Production</p>
              <h1 className="mt-3 text-4xl font-extrabold tracking-tight text-ink">Reel Automation Dashboard</h1>
              <p className="mt-3 max-w-2xl text-sm text-slate-600">
                Studio-quality reels generated entirely on your machine — 720p, CRF 22, ASS captions.
              </p>
            </div>
            <nav className="flex gap-3 rounded-full bg-slate-100 p-2">
              {tabs.map((tab) => (
                <button
                  key={tab}
                  type="button"
                  onClick={() => setActiveTab(tab)}
                  className={`rounded-full px-4 py-2 text-sm font-semibold capitalize transition ${
                    activeTab === tab ? "bg-white text-ink shadow" : "text-slate-500"
                  }`}
                >
                  {tab}
                </button>
              ))}
            </nav>
          </div>
        </header>

        {/* Error banner */}
        {error && (
          <div className="mt-6 rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            {error}
          </div>
        )}

        {/* Failure banner */}
        {jobFailed && jobResult?.error && (
          <div className="mt-4 rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            ❌ Generation failed: {jobResult.error}
          </div>
        )}

        <main className="mt-6">
          {activeTab === "dashboard" ? (
            <div className="space-y-6">

              {/* Pipeline progress — shown while job is running */}
              {isJobRunning && (
                <PipelineProgress currentStage={currentStage} logs={jobLogs} />
              )}

              {/* Video player — shown when job is done */}
              {jobCompleted && jobId && (
                <ReelPlayer jobId={jobId} topic={completedTopic} />
              )}

              {/* Main dashboard */}
              <Dashboard
                status={status}
                reels={reels}
                logs={logs}
                busy={busy}
                batchProgress={batchProgress}
                topic={topic}
                onTopicChange={setTopic}
                onStart={handleStart}
                onStop={() => runAction(stopAutomation)}
                onGenerate={handleGenerate}
                onGenerateBatch={() => runBatchGeneration(5)}
              />
            </div>
          ) : (
            <>
              {settingsSavedMsg && (
                <div className="mb-4 rounded-2xl border border-green-200 bg-green-50 px-4 py-3 text-sm font-semibold text-green-700">
                  {settingsSavedMsg}
                </div>
              )}
              <Settings
                initialValues={settings}
                onSave={async (values) => {
                  setBusy(true);
                  setError("");
                  setSettingsSavedMsg("");
                  try {
                    await updateSettings(values);
                    await refreshData();
                    setSettingsSavedMsg("Settings saved!");
                    window.setTimeout(() => setSettingsSavedMsg(""), 3000);
                  } catch (requestError) {
                    setError(extractErrorMessage(requestError, "Failed to save settings."));
                  } finally {
                    setBusy(false);
                  }
                }}
                busy={busy}
              />
            </>
          )}
        </main>
      </div>
    </div>
  );
}

export default App;
