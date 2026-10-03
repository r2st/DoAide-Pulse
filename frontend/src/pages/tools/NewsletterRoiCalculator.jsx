import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import ShareButtons from "../../components/ShareButtons";

const fmt = (n) =>
  n.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  });

const pct = (n) => `${n.toFixed(1)}%`;

export default function NewsletterRoiCalculator() {
  const [subscribers, setSubscribers] = useState(5000);
  const [openRate, setOpenRate] = useState(35);
  const [clickRate, setClickRate] = useState(5);
  const [conversionRate, setConversionRate] = useState(2);
  const [revenuePerConversion, setRevenuePerConversion] = useState(50);
  const [monthlyCost, setMonthlyCost] = useState(200);

  useEffect(() => {
    document.title = "Newsletter ROI Calculator | DoAide Pulse";
  }, []);

  const opens = subscribers * (openRate / 100);
  const clicks = opens * (clickRate / 100);
  const conversions = clicks * (conversionRate / 100);
  const monthlyRevenue = conversions * revenuePerConversion;
  const monthlyROI = monthlyCost > 0 ? ((monthlyRevenue - monthlyCost) / monthlyCost) * 100 : 0;
  const annualRevenue = monthlyRevenue * 12;

  return (
    <div className="mx-auto max-w-2xl px-4 py-10">
      <h1 className="page-title mb-2">Newsletter ROI Calculator</h1>
      <p className="mb-6 text-ink-500">
        Calculate the return on investment from your newsletter. Adjust the inputs to see projected revenue.
      </p>

      <div className="panel p-5 space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <label htmlFor="subs" className="label">Subscribers</label>
            <input id="subs" type="number" className="input" value={subscribers} onChange={(e) => setSubscribers(Number(e.target.value))} min={0} />
          </div>
          <div>
            <label htmlFor="open" className="label">Open rate (%)</label>
            <input id="open" type="number" className="input" value={openRate} onChange={(e) => setOpenRate(Number(e.target.value))} min={0} max={100} />
          </div>
          <div>
            <label htmlFor="click" className="label">Click rate (%)</label>
            <input id="click" type="number" className="input" value={clickRate} onChange={(e) => setClickRate(Number(e.target.value))} min={0} max={100} />
          </div>
          <div>
            <label htmlFor="conv" className="label">Conversion rate (%)</label>
            <input id="conv" type="number" className="input" value={conversionRate} onChange={(e) => setConversionRate(Number(e.target.value))} min={0} max={100} />
          </div>
          <div>
            <label htmlFor="rev" className="label">Revenue per conversion ($)</label>
            <input id="rev" type="number" className="input" value={revenuePerConversion} onChange={(e) => setRevenuePerConversion(Number(e.target.value))} min={0} />
          </div>
          <div>
            <label htmlFor="cost" className="label">Monthly cost ($)</label>
            <input id="cost" type="number" className="input" value={monthlyCost} onChange={(e) => setMonthlyCost(Number(e.target.value))} min={0} />
          </div>
        </div>
      </div>

      <div className="mt-6 grid gap-3 sm:grid-cols-3">
        <div className="panel p-4 text-center">
          <div className="eyebrow">Monthly opens</div>
          <div className="stat-figure mt-1" data-testid="opens">{Math.round(opens).toLocaleString()}</div>
        </div>
        <div className="panel p-4 text-center">
          <div className="eyebrow">Monthly clicks</div>
          <div className="stat-figure mt-1" data-testid="clicks">{Math.round(clicks).toLocaleString()}</div>
        </div>
        <div className="panel p-4 text-center">
          <div className="eyebrow">Conversions/mo</div>
          <div className="stat-figure mt-1" data-testid="conversions">{conversions.toFixed(1)}</div>
        </div>
        <div className="panel p-4 text-center">
          <div className="eyebrow">Monthly revenue</div>
          <div className="stat-figure mt-1 text-good" data-testid="revenue">{fmt(monthlyRevenue)}</div>
        </div>
        <div className="panel p-4 text-center">
          <div className="eyebrow">Monthly ROI</div>
          <div className={`stat-figure mt-1 ${monthlyROI >= 0 ? "text-good" : "text-bad"}`} data-testid="roi">{pct(monthlyROI)}</div>
        </div>
        <div className="panel p-4 text-center">
          <div className="eyebrow">Annual projection</div>
          <div className="stat-figure mt-1 text-brand-500" data-testid="annual">{fmt(annualRevenue)}</div>
        </div>
      </div>

      <div className="panel mt-10 p-5 text-center">
        <p className="mb-3 text-ink-900">Maximize your newsletter ROI with AI-powered optimization.</p>
        <Link to="/" className="btn-primary">Try DoAide Pulse</Link>
      </div>

      <div className="mt-6">
        <ShareButtons text="Calculate your newsletter ROI — free tool by DoAide Pulse" />
      </div>
    </div>
  );
}
