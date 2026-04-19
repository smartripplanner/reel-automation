import { useCallback, useEffect, useRef, useState } from "react";
import { getJobStatus } from "../services/api";

const POLL_INTERVAL_MS = 2500;
const TERMINAL_STATES = new Set(["completed", "failed"]);

/**
 * Poll a background reel-generation job until it reaches a terminal state.
 *
 * Usage:
 *   const { jobStatus, jobLogs, jobResult, startJob, clearJob } = useJobPoller();
 *
 *   // Start polling after you get a job_id from generateReelJob():
 *   startJob(jobId);
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
  const intervalRef = useRef(null);

  const stopPolling = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
  }, []);

  const poll = useCallback(async (id) => {
    try {
      const data = await getJobStatus(id);
      setJobStatus(data.status);
      setJobLogs(data.logs ?? []);
      if (TERMINAL_STATES.has(data.status)) {
        setJobResult(data.result ?? null);
        stopPolling();
      }
    } catch {
      // Network blip — keep polling
    }
  }, [stopPolling]);

  const startJob = useCallback((id) => {
    stopPolling();
    setJobId(id);
    setJobStatus("queued");
    setJobLogs([]);
    setJobResult(null);

    // Poll immediately, then on interval
    poll(id);
    intervalRef.current = setInterval(() => poll(id), POLL_INTERVAL_MS);
  }, [poll, stopPolling]);

  const clearJob = useCallback(() => {
    stopPolling();
    setJobId(null);
    setJobStatus(null);
    setJobLogs([]);
    setJobResult(null);
  }, [stopPolling]);

  // Cleanup on unmount
  useEffect(() => () => stopPolling(), [stopPolling]);

  return { jobId, jobStatus, jobLogs, jobResult, startJob, clearJob };
}
