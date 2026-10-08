import { Link } from "react-router-dom";

const MOCK_STATS = [
  { label: "Open Rate", value: "42.3%", trend: "+5.2%", up: true },
  { label: "Click Rate", value: "8.7%", trend: "+1.4%", up: true },
  { label: "Subscribers", value: "2,847", trend: "+312", up: true },
  { label: "Bounce Rate", value: "0.8%", trend: "-0.3%", up: false },
];

const MOCK_CHART_BARS = [
  { label: "Mon", height: 45 },
  { label: "Tue", height: 62 },
  { label: "Wed", height: 78 },
  { label: "Thu", height: 55 },
  { label: "Fri", height: 90 },
  { label: "Sat", height: 70 },
  { label: "Sun", height: 40 },
];

const MOCK_GROWTH = [
  { month: "Apr", value: 1200 },
  { month: "May", value: 1450 },
  { month: "Jun", value: 1680 },
  { month: "Jul", value: 1920 },
  { month: "Aug", value: 2340 },
  { month: "Sep", value: 2610 },
  { month: "Oct", value: 2847 },
];

function GrowthLine() {
  const max = Math.max(...MOCK_GROWTH.map((d) => d.value));
  const w = 280;
  const h = 80;
  const points = MOCK_GROWTH.map((d, i) => {
    const x = (i / (MOCK_GROWTH.length - 1)) * w;
    const y = h - (d.value / max) * h;
    return `${x},${y}`;
  });
  const line = points.join(" ");
  const areaPoints = `0,${h} ${line} ${w},${h}`;

  return (
    <svg viewBox={`0 0 ${w} ${h + 20}`} width="100%" height="100" className="block">
      <defs>
        <linearGradient id="growth-fill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#F0B429" stopOpacity="0.3" />
          <stop offset="100%" stopColor="#F0B429" stopOpacity="0" />
        </linearGradient>
      </defs>
      <polygon points={areaPoints} fill="url(#growth-fill)" />
      <polyline points={line} fill="none" stroke="#F0B429" strokeWidth="2" strokeLinejoin="round" />
      {MOCK_GROWTH.map((d, i) => (
        <text
          key={d.month}
          x={(i / (MOCK_GROWTH.length - 1)) * w}
          y={h + 16}
          fill="#6B7280"
          fontSize="9"
          textAnchor="middle"
        >
          {d.month}
        </text>
      ))}
    </svg>
  );
}

export default function AnalyticsMockup() {
  return (
    <div className="landing-analytics-mockup">
      <div className="landing-section-header">
        <span className="landing-section-eyebrow">Premium Analytics</span>
        <h2 className="landing-section-title">See what your audience does</h2>
        <p className="landing-section-subtitle">
          Track opens, clicks, subscriber growth, and engagement trends. Upgrade to unlock the full analytics dashboard.
        </p>
      </div>

      <div className="landing-analytics-dashboard">
        <div className="landing-analytics-blur-badge">Preview</div>

        <div className="landing-analytics-stats">
          {MOCK_STATS.map((s) => (
            <div key={s.label} className="landing-analytics-stat">
              <span className="landing-analytics-stat-label">{s.label}</span>
              <span className="landing-analytics-stat-value">{s.value}</span>
              <span
                className={`landing-analytics-stat-trend ${s.up ? "up" : "down"}`}
              >
                {s.trend}
              </span>
            </div>
          ))}
        </div>

        <div className="landing-analytics-charts">
          <div className="landing-analytics-chart-card">
            <div className="landing-analytics-chart-title">Opens this week</div>
            <div className="landing-analytics-bars">
              {MOCK_CHART_BARS.map((bar) => (
                <div key={bar.label} className="landing-analytics-bar-col">
                  <div
                    className="landing-analytics-bar"
                    style={{ height: `${bar.height}%` }}
                  />
                  <span className="landing-analytics-bar-label">{bar.label}</span>
                </div>
              ))}
            </div>
          </div>
          <div className="landing-analytics-chart-card">
            <div className="landing-analytics-chart-title">
              Subscriber growth
            </div>
            <GrowthLine />
          </div>
        </div>
      </div>

      <div className="landing-analytics-cta">
        <Link to="/login" className="landing-cta-primary">
          Start free — upgrade anytime
        </Link>
      </div>
    </div>
  );
}
