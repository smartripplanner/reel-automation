const ReelList = ({ reels }) => {
  return (
    <div className="rounded-[2rem] border border-slate-200 bg-white/80 p-6 shadow-panel backdrop-blur">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xl font-bold text-ink">Reel History</h3>
          <p className="text-sm text-slate-500">Latest generated files stored locally.</p>
        </div>
      </div>

      <div className="mt-5 space-y-3">
        {reels.length === 0 ? (
          <div className="rounded-2xl border border-dashed border-slate-300 p-5 text-sm text-slate-500">
            No reels generated yet.
          </div>
        ) : (
          reels.map((reel) => (
            <div key={reel.id} className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
              <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
                <div>
                  <p className="font-semibold text-ink">{reel.file_path.split("/").pop()}</p>
                  <p className="text-sm text-slate-500">{reel.caption}</p>
                </div>
                <div className="text-sm text-slate-500">
                  <span className="rounded-full bg-white px-3 py-1 font-medium text-slate-700">{reel.status}</span>
                </div>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
};

export default ReelList;
