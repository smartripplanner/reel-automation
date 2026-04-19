import { useEffect, useState } from "react";

import Dashboard from "./pages/Dashboard";
import Settings from "./pages/Settings";
import { useJobPoller } from "./hooks/useJobPoller";
import {
  generateBatchReels,
  generateReelJob,
  getAutomationStatus,
  getLogs,
  getReels,
  getSettings,
  startAutomation,
  stopAutomation,
  updateSettings,
} from "./services/api";

const initialSettings = {
  niche: "Motivation",
  reel_duration: 30,
  reels_per_day: 3,
};

const tabs = ["dashboard", "settings"];

/**
 * Normalise a FastAPI / axios error into a plain string.
 *
 * FastAPI 422 Unprocessable Content responses carry `detail` as an array of
 * validation objects: [{loc, msg, type}, ...].  Passing that array directly
 * to React state and then rendering it as {error} crashes with
 * "Objects are not valid as a React child".  This helper flattens every
 * shape FastAPI (or any other API error) might return into a displayable
 * string.
 */
const extractErrorMessage = (err, fallback = "Request failed. Please try again.") => {
  const detail = err?.response?.data?.detail;
  if (!detail) return fallback;
  if (Array.isArray(detail)) {
    // FastAPI validation errors — join all human-readable msg fields
    return detail.map((e) => e?.msg ?? JSON.stringify(e)).join("; ");
  }
  if (typeof detail === "string") return detail;
  return fallback;
};

function App() {
  const [activeTab, setActiveTab] = useState("dashboard");
  const [status, setStatus] = useState(null);
  const [reels, setReels] = useState([]);
  const [logs, setLogs] = useState([]);
  const [settings, setSettings] = useState(initialSettings);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [batchProgress, setBatchProgress] = useState("");

  const { jobStatus, jobLogs, jobResult, startJob, clearJob } = useJobPoller();
  const [settingsSavedMsg, setSettingsSavedMsg] = useState("");

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
      setError("Unable to reach the backend. Start FastAPI on port 8000 and refresh.");
    });
  }, []);

  // Refresh data when an async job finishes
  useEffect(() => {
    if (jobStatus === "completed" || jobStatus === "failed") {
      setBusy(false);
      refreshData().catch(() => {});
    }
  }, [jobStatus]);

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

  // Async generate — returns job_id immediately, polls until done
  const handleGenerate = async () => {
    setBusy(true);
    setError("");
    clearJob();

    try {
      const { job_id } = await generateReelJob();
      startJob(job_id);
      // busy stays true until the useEffect above sees terminal status
    } catch (requestError) {
      setError(extractErrorMessage(requestError, "Failed to start generation."));
      setBusy(false);
    }
  };

  const runBatchGeneration = async (count) => {
    setBusy(true);
    setError("");
    setBatchProgress(`Generating ${count} reels. This can take a few minutes...`);
    let progressIntervalId = null;

    try {
      progressIntervalId = window.setInterval(async () => {
        try {
          const latestLogs = await getLogs();
          const progressLog = latestLogs.find((log) =>
            /Starting reel \d+ of \d+|Finished reel \d+ of \d+/.test(log.message)
          );
          if (progressLog) {
            setBatchProgress(progressLog.message);
          }
        } catch {
          // Keep existing progress message on poll failure
        }
      }, 2000);

      await generateBatchReels(count);
      setBatchProgress(`Finished generating ${count} reels.`);
      await refreshData();
      window.setTimeout(() => setBatchProgress(""), 3000);
    } catch (requestError) {
      setError(extractErrorMessage(requestError, "Batch generation failed. Please try again."));
      setBatchProgress("");
    } finally {
      if (progressIntervalId) {
        window.clearInterval(progressIntervalId);
      }
      setBusy(false);
    }
  };

  // Derive a live progress message for the generate button
  const generateProgress = (() => {
    if (!jobStatus || jobStatus === "completed" || jobStatus === "failed") return "";
    const last = jobLogs[jobLogs.length - 1];
    if (jobStatus === "queued") return "Queued...";
    return last ?? "Running...";
  })();

  return (
    <div className="min-h-screen bg-[radial-gradient(circle_at_top_left,_rgba(249,115,22,0.16),_transparent_34%),linear-gradient(180deg,_#f8fafc_0%,_#eff6ff_100%)]">
      <div className="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
        <header className="rounded-[2rem] border border-white/60 bg-white/75 p-6 shadow-panel backdrop-blur">
          <div className="flex flex-col gap-6 lg:flex-row lg:items-end lg:justify-between">
            <div>
              <p className="text-sm font-semibold uppercase tracking-[0.3em] text-accent">Local MVP</p>
              <h1 className="mt-3 text-4xl font-extrabold tracking-tight text-ink">Reel Automation Dashboard</h1>
              <p className="mt-3 max-w-2xl text-sm text-slate-600">
                Manage manual reel generation, monitor automation state, and review local history from one place.
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

        {error ? (
          <div className="mt-6 rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</div>
        ) : null}

        {generateProgress ? (
          <div className="mt-4 rounded-2xl border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-700">
            {generateProgress}
          </div>
        ) : null}

        {jobStatus === "failed" && jobResult?.error ? (
          <div className="mt-4 rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            Generation failed: {jobResult.error}
          </div>
        ) : null}

        <main className="mt-6">
          {activeTab === "dashboard" ? (
            <Dashboard
              status={status}
              reels={reels}
              logs={logs}
              busy={busy}
              batchProgress={batchProgress}
              onStart={() => runAction(startAutomation)}
              onStop={() => runAction(stopAutomation)}
              onGenerate={handleGenerate}
              onGenerateBatch={() => runBatchGeneration(5)}
            />
          ) : (
            <>
              {settingsSavedMsg ? (
                <div className="mb-4 rounded-2xl border border-green-200 bg-green-50 px-4 py-3 text-sm font-semibold text-green-700">
                  {settingsSavedMsg}
                </div>
              ) : null}
              <Settings
                initialValues={settings}
                onSave={async (values) => {
                  setBusy(true);
                  setError("");
                  setSettingsSavedMsg("");
                  try {
                    await updateSettings(values);
                    await refreshData();
                    setSettingsSavedMsg("Settings saved successfully!");
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
