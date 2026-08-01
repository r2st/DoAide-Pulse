import { useEffect, useState } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../hooks/useAuth";
import { api } from "../lib/api";
import Logo from "./ui/Logo";

// The six destinations, in the order the work happens: see where things stand,
// register what to write about, write it, place it in time, ship it, configure.
const TABS = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/projects", label: "Projects" },
  { to: "/triggers", label: "Triggers" },
  { to: "/content", label: "Content" },
  { to: "/calendar", label: "Calendar" },
  { to: "/publish", label: "Publish" },
  { to: "/settings", label: "Settings" },
];

/**
 * The only chrome in the app: wordmark, destinations, account.
 *
 * Two tabs carry counts of things waiting on the user — drafts in review, and
 * publications that failed. Both are things nothing else will resolve on its
 * own, which is the bar for earning a badge.
 */
export default function Shell({ children }) {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);
  const counts = useNavCounts(location.pathname);

  useEffect(() => {
    setMenuOpen(false);
  }, [location.pathname]);

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-20 border-b border-line bg-canvas/85 backdrop-blur-xl">
        <div className="mx-auto flex h-14 max-w-6xl items-center justify-between px-5">
          <div className="flex min-w-0 items-center gap-5 sm:gap-8">
            <Wordmark />
            <nav className="hidden items-center gap-1 overflow-x-auto md:flex">
              {TABS.map((tab) => (
                <Tab key={tab.to} to={tab.to} end={tab.end} badge={badgeFor(tab, counts)}>
                  {tab.label}
                </Tab>
              ))}
            </nav>
          </div>

          <div className="flex shrink-0 items-center gap-3">
            <span className="hidden font-mono text-xs text-ink-400 lg:block">
              {user?.email}
            </span>
            <button
              className="btn-quiet hidden md:inline-flex"
              onClick={() => {
                logout();
                navigate("/login");
              }}
            >
              Sign out
            </button>

            <button
              className="btn-quiet -mr-1 inline-flex md:hidden"
              onClick={() => setMenuOpen((v) => !v)}
              aria-expanded={menuOpen}
              aria-label="Toggle navigation menu"
            >
              <MenuGlyph open={menuOpen} />
              {counts.review + counts.failed > 0 && !menuOpen && (
                <span
                  className="ml-1 h-1.5 w-1.5 rounded-full bg-brand-500"
                  aria-hidden="true"
                />
              )}
            </button>
          </div>
        </div>

        {menuOpen && (
          <nav className="border-t border-line px-3 py-2 md:hidden">
            {TABS.map((tab) => (
              <MobileTab
                key={tab.to}
                to={tab.to}
                end={tab.end}
                badge={badgeFor(tab, counts)}
              >
                {tab.label}
              </MobileTab>
            ))}
            <button
              className="mt-1 block w-full rounded-md px-3 py-2.5 text-left text-sm text-ink-500 hover:bg-ink-900/[0.04] hover:text-ink-900"
              onClick={() => {
                logout();
                navigate("/login");
              }}
            >
              Sign out
            </button>
          </nav>
        )}
      </header>

      <main className="mx-auto max-w-6xl px-5 py-9">{children}</main>
    </div>
  );
}

/**
 * Poll the two queues the badges show. Refreshed on navigation so approving a
 * draft updates the count on the way out, plus a slow timer for the case where
 * the autopilot writes something while the tab is open.
 */
function useNavCounts(pathname) {
  const [counts, setCounts] = useState({ review: 0, failed: 0 });

  useEffect(() => {
    let cancelled = false;
    const fetchCounts = () =>
      Promise.all([
        api.reviewQueue().catch(() => []),
        api.publicationQueue().catch(() => []),
      ]).then(([review, queue]) => {
        if (cancelled) return;
        setCounts({
          review: review?.length ?? 0,
          failed: (queue ?? []).filter((p) => p.status === "failed").length,
        });
      });

    fetchCounts();
    const timer = setInterval(fetchCounts, 60000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [pathname]);

  return counts;
}

function badgeFor(tab, counts) {
  if (tab.to === "/content" && counts.review > 0) {
    return { value: counts.review, label: "awaiting review" };
  }
  if (tab.to === "/publish" && counts.failed > 0) {
    return { value: counts.failed, label: "failed publications" };
  }
  return null;
}

function Wordmark() {
  return (
    <span className="flex shrink-0 items-center gap-2.5">
      <Logo className="h-[26px] w-[26px] shrink-0 text-brand-500" title="Herald" />
      <span className="font-display text-xl text-ink-900">Herald</span>
    </span>
  );
}

function Tab({ to, end, badge, children }) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        [
          "relative shrink-0 whitespace-nowrap rounded-md py-1.5 text-sm transition-colors",
          // A badged tab reserves space for its own count so it can't sit on
          // top of the next label.
          badge ? "pl-3 pr-7" : "px-3",
          isActive ? "text-ink-900" : "text-ink-500 hover:text-ink-900",
        ].join(" ")
      }
    >
      {({ isActive }) => (
        <>
          {children}
          {badge && <CountBadge {...badge} />}
          {isActive && (
            <span className="absolute inset-x-3 -bottom-[13px] h-0.5 rounded-full bg-brand-500" />
          )}
        </>
      )}
    </NavLink>
  );
}

function MobileTab({ to, end, badge, children }) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        [
          "flex items-center justify-between rounded-md px-3 py-2.5 text-sm transition-colors",
          isActive
            ? "bg-brand-50 text-ink-900"
            : "text-ink-500 hover:bg-ink-900/[0.04] hover:text-ink-900",
        ].join(" ")
      }
    >
      <span>{children}</span>
      {badge && <CountBadge {...badge} inline />}
    </NavLink>
  );
}

function CountBadge({ value, label, inline = false }) {
  return (
    <span
      className={[
        inline ? "" : "absolute right-1 top-1/2 -translate-y-1/2",
        "inline-flex min-w-[1.15rem] items-center justify-center rounded-full bg-brand-500 px-1",
        "font-mono text-[10px] font-semibold leading-[1.15rem] text-white",
      ].join(" ")}
      aria-label={`${value} ${label}`}
    >
      {value > 9 ? "9+" : value}
    </span>
  );
}

function MenuGlyph({ open }) {
  return (
    <svg
      viewBox="0 0 20 20"
      className="h-4 w-4"
      aria-hidden="true"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
    >
      {open ? (
        <>
          <path d="M5 5l10 10" />
          <path d="M15 5L5 15" />
        </>
      ) : (
        <>
          <path d="M3 6h14" />
          <path d="M3 10h14" />
          <path d="M3 14h14" />
        </>
      )}
    </svg>
  );
}
