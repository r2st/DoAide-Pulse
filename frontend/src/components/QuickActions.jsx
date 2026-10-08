import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useToast } from "./ui/Toast";
import { api } from "../lib/api";

export default function QuickActions({ reviewCount, onApproved }) {
  const navigate = useNavigate();
  const toast = useToast();
  const [approving, setApproving] = useState(false);

  async function approveAll() {
    if (reviewCount === 0) return;
    setApproving(true);
    try {
      const queue = await api.reviewQueue();
      const ids = queue.map((item) => item.id);
      if (ids.length === 0) {
        toast.info("Nothing to approve");
        return;
      }
      const result = await api.bulkApprove(ids);
      const ok = result.succeeded?.length ?? 0;
      const fail = result.failed?.length ?? 0;
      if (ok > 0) toast.success(`Approved ${ok} item${ok === 1 ? "" : "s"}`);
      if (fail > 0) toast.error(`${fail} item${fail === 1 ? "" : "s"} could not be approved`);
      onApproved?.();
    } catch (err) {
      toast.error(err.message);
    } finally {
      setApproving(false);
    }
  }

  return (
    <div
      className="fixed bottom-6 left-1/2 z-30 -translate-x-1/2 md:left-[calc(50%+7rem)]"
      role="toolbar"
      aria-label="Quick actions"
    >
      <div className="flex items-center gap-2 rounded-2xl border border-line bg-paper/95 px-3 py-2 shadow-pop backdrop-blur-xl">
        <button
          className="quick-action-btn bg-brand-500 text-canvas hover:bg-brand-600"
          onClick={() => navigate("/content")}
          title="Generate new content"
        >
          <PlusIcon />
          <span className="hidden sm:inline">Generate</span>
        </button>

        {reviewCount > 0 && (
          <button
            className="quick-action-btn bg-good text-canvas hover:bg-good/90"
            onClick={approveAll}
            disabled={approving}
            title={`Approve all ${reviewCount} reviews`}
          >
            <CheckAllIcon />
            <span className="hidden sm:inline">
              {approving ? "Approving…" : `Approve ${reviewCount}`}
            </span>
          </button>
        )}

        <Link
          to="/content?status=published"
          className="quick-action-btn border border-line text-ink-600 hover:bg-canvas"
          title="View published content"
        >
          <PublishedIcon />
          <span className="hidden sm:inline">Published</span>
        </Link>

        <Link
          to="/settings"
          className="quick-action-btn border border-line text-ink-600 hover:bg-canvas"
          title="Settings"
        >
          <GearIcon />
        </Link>
      </div>
    </div>
  );
}

function PlusIcon() {
  return (
    <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <path d="M8 3v10M3 8h10" />
    </svg>
  );
}

function CheckAllIcon() {
  return (
    <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 8.5l3 3 5-6" />
      <path d="M8 11.5l1.5 1.5L15 6" />
    </svg>
  );
}

function PublishedIcon() {
  return (
    <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 4l6-2 6 2v6l-6 4-6-4V4z" />
      <path d="M8 8v6M2 4l6 4 6-4" />
    </svg>
  );
}

function GearIcon() {
  return (
    <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="8" r="2.5" />
      <path d="M8 1v2M8 13v2M2.5 2.5l1.4 1.4M12.1 12.1l1.4 1.4M1 8h2M13 8h2M2.5 13.5l1.4-1.4M12.1 3.9l1.4-1.4" />
    </svg>
  );
}
