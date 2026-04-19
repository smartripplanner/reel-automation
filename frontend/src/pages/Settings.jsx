import { useEffect, useState } from "react";

const Settings = ({ initialValues, onSave, busy }) => {
  const [form, setForm] = useState(initialValues);

  useEffect(() => {
    setForm(initialValues);
  }, [initialValues]);

  const handleChange = (event) => {
    const { name, value } = event.target;
    setForm((current) => ({
      ...current,
      [name]: name === "niche" ? value : Number(value),
    }));
  };

  const handleSubmit = async (event) => {
    event.preventDefault();
    await onSave(form);
  };

  return (
    <form onSubmit={handleSubmit} className="rounded-[2rem] border border-slate-200 bg-white/80 p-6 shadow-panel backdrop-blur">
      <div>
        <h2 className="text-2xl font-bold text-ink">Automation Settings</h2>
        <p className="text-sm text-slate-500">Update the reel generation defaults stored in SQLite.</p>
      </div>

      <div className="mt-6 grid gap-5">
        <label className="grid gap-2">
          <span className="text-sm font-semibold text-slate-600">Niche</span>
          <textarea
            name="niche"
            rows={3}
            value={form.niche}
            onChange={handleChange}
            placeholder="e.g. Hidden aesthetic &amp; budget-friendly places across Europe — generate a unique reel each time with different destinations…"
            className="rounded-2xl border border-slate-200 bg-white px-4 py-3 outline-none ring-0 transition focus:border-sea resize-none leading-relaxed"
          />
          <span className="text-xs text-slate-400">{form.niche?.length ?? 0} / 500 characters</span>
        </label>

        <label className="grid gap-2">
          <span className="text-sm font-semibold text-slate-600">Reel Duration (seconds)</span>
          <input
            type="number"
            name="reel_duration"
            min="5"
            max="300"
            value={form.reel_duration}
            onChange={handleChange}
            className="rounded-2xl border border-slate-200 bg-white px-4 py-3 outline-none ring-0 transition focus:border-sea"
          />
        </label>

        <label className="grid gap-2">
          <span className="text-sm font-semibold text-slate-600">Reels Per Day</span>
          <input
            type="number"
            name="reels_per_day"
            min="1"
            max="50"
            value={form.reels_per_day}
            onChange={handleChange}
            className="rounded-2xl border border-slate-200 bg-white px-4 py-3 outline-none ring-0 transition focus:border-sea"
          />
        </label>
      </div>

      <button
        type="submit"
        disabled={busy}
        className="mt-6 rounded-2xl bg-ink px-5 py-3 text-sm font-semibold text-white transition hover:-translate-y-0.5 disabled:cursor-not-allowed disabled:opacity-60"
      >
        Save Settings
      </button>
    </form>
  );
};

export default Settings;
