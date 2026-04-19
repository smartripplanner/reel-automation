const StatusCard = ({ title, value, detail, tone = "default" }) => {
  const toneMap = {
    default: "from-white to-slate-50",
    success: "from-emerald-50 to-white",
    accent: "from-orange-50 to-white",
  };

  return (
    <div className={`rounded-3xl border border-white/70 bg-gradient-to-br ${toneMap[tone]} p-5 shadow-panel`}>
      <p className="text-sm font-semibold uppercase tracking-[0.2em] text-slate-500">{title}</p>
      <p className="mt-3 text-3xl font-extrabold text-ink">{value}</p>
      <p className="mt-2 text-sm text-slate-500">{detail}</p>
    </div>
  );
};

export default StatusCard;
