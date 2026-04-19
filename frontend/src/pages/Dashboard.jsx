import ControlPanel from "../components/ControlPanel";
import ReelList from "../components/ReelList";
import StatusCard from "../components/StatusCard";

const Dashboard = ({ status, reels, logs, onStart, onStop, onGenerate, onGenerateBatch, busy, batchProgress }) => {
  const reelsToday = reels.filter((reel) => {
    const createdAt = new Date(reel.created_at);
    const now = new Date();
    return createdAt.toDateString() === now.toDateString();
  }).length;

  return (
    <div className="space-y-6">
      <section className="grid gap-4 md:grid-cols-3">
        <StatusCard
          title="Automation"
          value={status?.is_running ? "Running" : "Stopped"}
          detail={`Mode: ${status?.mode ?? "manual"}`}
          tone={status?.is_running ? "success" : "default"}
        />
        <StatusCard
          title="Reels Today"
          value={reelsToday}
          detail="Generated during the current local day"
          tone="accent"
        />
        <StatusCard
          title="Last Run"
          value={status?.last_run_at ? new Date(status.last_run_at).toLocaleTimeString() : "Not yet"}
          detail={status?.active_job ? `Active job: ${status.active_job}` : "No active job"}
        />
      </section>

      <ControlPanel
        onStart={onStart}
        onStop={onStop}
        onGenerate={onGenerate}
        onGenerateBatch={onGenerateBatch}
        busy={busy}
        batchProgress={batchProgress}
      />

      <section className="grid gap-6 xl:grid-cols-[1.4fr_1fr]">
        <ReelList reels={reels} />

        <div className="rounded-[2rem] border border-slate-200 bg-white/80 p-6 shadow-panel backdrop-blur">
          <h3 className="text-xl font-bold text-ink">Activity Logs</h3>
          <p className="text-sm text-slate-500">Pipeline and system events from the local database.</p>

          <div className="mt-5 space-y-3">
            {logs.length === 0 ? (
              <div className="rounded-2xl border border-dashed border-slate-300 p-5 text-sm text-slate-500">
                No logs recorded yet.
              </div>
            ) : (
              logs.map((log) => (
                <div key={log.id} className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
                  <p className="text-sm font-medium text-ink">{log.message}</p>
                  <p className="mt-1 text-xs uppercase tracking-[0.14em] text-slate-400">
                    {new Date(log.timestamp).toLocaleString()}
                  </p>
                </div>
              ))
            )}
          </div>
        </div>
      </section>
    </div>
  );
};

export default Dashboard;
