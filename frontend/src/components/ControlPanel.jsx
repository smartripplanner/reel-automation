const baseButton =
  "rounded-2xl px-4 py-3 text-sm font-semibold transition hover:-translate-y-0.5 focus:outline-none focus:ring-2 focus:ring-offset-2";

const ControlPanel = ({
  onStart,
  onStop,
  onGenerate,
  onGenerateBatch,
  busy,
  batchProgress,
  topic,
  onTopicChange,
}) => {
  return (
    <div className="rounded-[2rem] border border-slate-200 bg-white/80 p-6 shadow-panel backdrop-blur">

      {/* Topic input */}
      <div className="mb-5">
        <label htmlFor="topic-input" className="block text-sm font-semibold text-ink mb-2">
          Topic <span className="font-normal text-slate-400">(optional — leave blank to auto-generate)</span>
        </label>
        <input
          id="topic-input"
          type="text"
          value={topic}
          onChange={(e) => onTopicChange?.(e.target.value)}
          disabled={busy}
          placeholder="e.g. Best hidden gems in Europe 2026"
          className="w-full rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-ink placeholder-slate-400 focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/30 disabled:cursor-not-allowed disabled:opacity-60 transition"
        />
      </div>

      {/* Batch progress notice */}
      {batchProgress && (
        <div className="mb-4 rounded-2xl border border-orange-200 bg-orange-50 px-4 py-3 text-sm font-medium text-orange-800">
          {batchProgress}
        </div>
      )}

      {/* Action buttons */}
      <div className="flex flex-col gap-3 sm:flex-row sm:flex-wrap">
        <button
          type="button"
          onClick={onGenerate}
          disabled={busy}
          className={`${baseButton} bg-accent text-white focus:ring-accent disabled:cursor-not-allowed disabled:opacity-60 flex items-center gap-2`}
        >
          {busy ? (
            <>
              <span className="inline-block h-4 w-4 rounded-full border-2 border-white/40 border-t-white animate-spin" />
              Generating...
            </>
          ) : (
            <>🎬 Generate Reel</>
          )}
        </button>

        <button
          type="button"
          onClick={onStart}
          disabled={busy}
          className={`${baseButton} bg-sea text-white focus:ring-sea disabled:cursor-not-allowed disabled:opacity-60`}
        >
          ▶ Start Automation
        </button>

        <button
          type="button"
          onClick={onStop}
          disabled={busy}
          className={`${baseButton} bg-slate-900 text-white focus:ring-slate-900 disabled:cursor-not-allowed disabled:opacity-60`}
        >
          ⏹ Stop Automation
        </button>

        <button
          type="button"
          onClick={onGenerateBatch}
          disabled={busy}
          className={`${baseButton} bg-white text-ink ring-1 ring-slate-200 focus:ring-ink disabled:cursor-not-allowed disabled:opacity-60`}
        >
          📦 Generate 5 Reels
        </button>
      </div>
    </div>
  );
};

export default ControlPanel;
