const baseButton =
  "rounded-2xl px-4 py-3 text-sm font-semibold transition hover:-translate-y-0.5 focus:outline-none focus:ring-2 focus:ring-offset-2";

const ControlPanel = ({ onStart, onStop, onGenerate, onGenerateBatch, busy, batchProgress }) => {
  return (
    <div className="rounded-[2rem] border border-slate-200 bg-white/80 p-6 shadow-panel backdrop-blur">
      {batchProgress ? (
        <div className="mb-4 rounded-2xl border border-orange-200 bg-orange-50 px-4 py-3 text-sm font-medium text-orange-800">
          {batchProgress}
        </div>
      ) : null}

      <div className="flex flex-col gap-4 md:flex-row">
        <button
          type="button"
          onClick={onStart}
          disabled={busy}
          className={`${baseButton} bg-sea text-white focus:ring-sea disabled:cursor-not-allowed disabled:opacity-60`}
        >
          Start Automation
        </button>
        <button
          type="button"
          onClick={onStop}
          disabled={busy}
          className={`${baseButton} bg-slate-900 text-white focus:ring-slate-900 disabled:cursor-not-allowed disabled:opacity-60`}
        >
          Stop Automation
        </button>
        <button
          type="button"
          onClick={onGenerate}
          disabled={busy}
          className={`${baseButton} bg-accent text-white focus:ring-accent disabled:cursor-not-allowed disabled:opacity-60`}
        >
          Generate Reel
        </button>
        <button
          type="button"
          onClick={onGenerateBatch}
          disabled={busy}
          className={`${baseButton} bg-white text-ink ring-1 ring-slate-200 focus:ring-ink disabled:cursor-not-allowed disabled:opacity-60`}
        >
          Generate 5 Reels
        </button>
      </div>
    </div>
  );
};

export default ControlPanel;
