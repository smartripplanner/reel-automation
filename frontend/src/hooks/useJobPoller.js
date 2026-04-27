import { useCallback, useEffect, useRef, useState } from "react";
import { getJobStatus } from "../services/api";

// Base poll interval. After the job has been running a while we back off
// to reduce server load and log spam (see _nextInterval below).
const BASE_INTERVAL_MS = 5_000;

// How many consecutive 404s before we give up and surface an error.
// After a Render restart, the job ID is gone from memory but now recovered
// in DB — so we should almost never hit this. Keep it as a last-resort guard.
const MAX_NOT_FOUND_ERRORS = 3;

const TERMINAL_STATES = new Set(["completed", "failed"]);

/**
 * Exponential-ish back-off based on elapsed time since the job started.
 *   0–60 s   → poll every 5 s   (job is probably just warming up)
 *   60–180 s → poll every 8 s   (pipeline is running)
 *   180+ s   → poll every 15 s  (long render / whisper pass)
 */
function _nextInterval(startedAt) {
  if (!startedAt) return BASE_INTERVAL_MS;
  const elapsed = Date.now() - startedAt;
  if (elapsed < 60_000) return BASE_INTERVAL_MS;          // 5 s
  if (elapsed < 180_000) return 8_000;                    // 8 s
  return 15_000;                                          // 15 s
}

/**
 * Poll a background reel-generation job until it reaches a terminal state.
 *
 * Usage:
 *   const { jobStatus, jobLogs, jobResult, startJob, clearJob } = useJobPoller();
 *   startJob(jobId);   // begins adaptive polling
 *
 * Returns:
 *   jobStatus  — null | "queued" | "running" | "completed" | "failed"
 *   jobLogs    — string[] of progress messages
 *   jobResult  — result dict on completion, null otherwise
 *   startJob   — (jobId: string) => void
 *   clearJob   — () => void  (reset all state)
 */
export function useJobPoller() {
  const [jobId, setJobId] = useState(null);
  const [jobStatus, setJobStatus] = useState(null);
  const [jobLogs, setJobLogs] = useState([]);
  const [jobResult, setJobResult] = useState(null);

  const timerRef = useRef(null);            // setTimeout handle (not setInterval)
  const notFoundCountRef = useRef(0);       // consecutive 404 counter
  const startedAtRef = useRef(null);        // when polling began (for back-off)
  const activeJobIdRef = useRef(null);      // guards against stale closures

  const stopPolling = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  // Recursive adaptive poller — schedules itself after each response
  const scheduleNext = useCallback((id) => {
    stopPolling();
    const delay = _nextInterval(startedAtRef.current);
    timerRef.current = setTimeout(async () => {
      // Guard: if startJob was called again with a different id, abort
      if (activeJobIdRef.current !== id) return;

      try {
        const data = await getJobStatus(id);
        notFoundCountRef.current = 0;   // reset on success
        setJobStatus(data.status);
        setJobLogs(data.logs ?? []);

        if (TERMINAL_STATES.has(data.status)) {
          setJobResult(data.result ?? null);
          stopPolling();
          return;
        }
      } catch (err) {
        const status = err?.response?.status;
        if (status === 404) {
          notFoundCountRef.current += 1;
          if (notFoundCountRef.current >= MAX_NOT_FOUND_ERRORS) {
            // Job gone — likely a server restart that didn't mark the job failed
            // in DB yet (race). Surface a meaningful error and stop.
            setJobStatus("failed");
            setJobResult({ error: "Job not found — the server may have restarted. Please try again." });
            stopPolling();
            return;
          }
        }
        // Any other network error: keep polling (transient blip)
      }

      scheduleNext(id);   // schedule next tick
    }, delay);
  }, [stopPolling]);

  const startJob = useCallback((id) => {
    stopPolling();
    activeJobIdRef.current = id;
    notFoundCountRef.current = 0;
    startedAtRef.current = Date.now();

    setJobId(id);
    setJobStatus("queued");
    setJobLogs([]);
    setJobResult(null);

    // Fire one poll immediately, then the adaptive loop takes over
    (async () => {
      try {
        const data = await getJobStatus(id);
        notFoundCountRef.current = 0;
        setJobStatus(data.status);
        setJobLogs(data.logs ?? []);
        if (TERMINAL_STATES.has(data.status)) {
          setJobResult(data.result ?? null);
          return;
        }
      } catch {
        /* swallow — scheduleNext will retry */
      }
      scheduleNext(id);
    })();
  }, [scheduleNext, stopPolling]);

  const clearJob = useCallback(() => {
    stopPolling();
    activeJobIdRef.current = null;
    notFoundCountRef.current = 0;
    startedAtRef.current = null;
    setJobId(null);
    setJobStatus(null);
    setJobLogs([]);
    setJobResult(null);
  }, [stopPolling]);

  // Cleanup on unmount
  useEffect(() => () => stopPolling(), [stopPolling]);

  return { jobId, jobStatus, jobLogs, jobResult, startJob, clearJob };
}
