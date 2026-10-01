import { useEffect, useState } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../hooks/useAuth";
import { api } from "../lib/api";
import Logo from "./ui/Logo";

const TABS = [
  { to: "/", label: "Dashboard", icon: DashboardIcon, end: true },
  { to: "/projects", label: "Projects", icon: ProjectsIcon },
  { to: "/triggers", label: "Triggers", icon: TriggersIcon },
  { to: "/templates", label: "Templates", icon: TemplatesIcon },
  { to: "/content", label: "Content", icon: ContentIcon },
  { to: "/calendar", label: "Calendar", icon: CalendarIcon },
  { to: "/publish", label: "Publish", icon: PublishIcon },
  { to: "/analytics", label: "Analytics", icon: AnalyticsIcon },
  { to: "/settings", label: "Settings", icon: SettingsIcon },
];

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
    <div className="flex min-h-screen bg-canvas">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-3 focus:z-50 focus:rounded-md focus:bg-brand-500 focus:px-3 focus:py-1.5 focus:text-sm focus:text-canvas"
      >
        Skip to content
      </a>

      {/* Desktop sidebar */}
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-56 flex-col border-r border-line bg-canvas md:flex">
        <div className="flex items-center gap-2.5 px-5 py-5">
          <Logo className="h-8 w-8 shrink-0" title="Pulse" />
          <span className="font-display text-xl text-ink-900">Pulse</span>
        </div>

        <nav aria-label="Main" className="flex-1 space-y-0.5 px-3 py-2">
          {TABS.map((tab) => (
            <SidebarTab key={tab.to} tab={tab} badge={badgeFor(tab, counts)} />
          ))}
        </nav>

        <div className="border-t border-line px-4 py-4">
          <div className="mb-3 truncate font-mono text-[11px] text-ink-400">
            {user?.email}
          </div>
          <button
            className="w-full rounded-lg border border-line px-3 py-2 text-xs text-ink-500 transition-colors hover:border-line-strong hover:text-ink-900"
            onClick={() => {
              logout();
              navigate("/login");
            }}
          >
            Sign out
          </button>
        </div>
      </aside>

      {/* Mobile header */}
      <header className="fixed inset-x-0 top-0 z-20 flex h-14 items-center justify-between border-b border-line bg-canvas/95 px-4 backdrop-blur-xl md:hidden">
        <div className="flex items-center gap-2.5">
          <Logo className="h-7 w-7 shrink-0" title="Pulse" />
          <span className="font-display text-lg text-ink-900">Pulse</span>
        </div>
        <button
          className="btn-quiet -mr-1"
          onClick={() => setMenuOpen((v) => !v)}
          aria-expanded={menuOpen}
          aria-controls="mobile-nav"
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
      </header>

      {menuOpen && (
        <nav
          id="mobile-nav"
          aria-label="Main"
          className="fixed inset-x-0 top-14 z-20 border-b border-line bg-canvas px-3 py-2 md:hidden"
        >
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
            className="mt-1 block w-full rounded-md px-3 py-2.5 text-left text-sm text-ink-500 hover:bg-ink-400/10 hover:text-ink-900"
            onClick={() => {
              logout();
              navigate("/login");
            }}
          >
            Sign out
          </button>
        </nav>
      )}

      {/* Main content area */}
      <div className="flex min-w-0 flex-1 flex-col md:ml-56">
        <main id="main" tabIndex={-1} className="flex-1 px-6 py-8 pt-20 md:px-8 md:pt-8">
          <div className="mx-auto max-w-5xl">
            {children}
          </div>
        </main>

        <footer className="border-t border-line py-4 text-center text-xs text-ink-400">
          Pulse by{" "}
          <a
            href="https://doaide.com"
            className="underline-offset-4 hover:text-ink-500 hover:underline"
            target="_blank"
            rel="noopener noreferrer"
          >
            DoAide
          </a>
        </footer>
      </div>
    </div>
  );
}

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

function SidebarTab({ tab, badge }) {
  const Icon = tab.icon;
  return (
    <NavLink
      to={tab.to}
      end={tab.end}
      className={({ isActive }) =>
        [
          "group flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors",
          isActive
            ? "bg-brand-50 text-brand-500 font-medium"
            : "text-ink-500 hover:bg-ink-400/10 hover:text-ink-900",
        ].join(" ")
      }
    >
      {({ isActive }) => (
        <>
          <Icon active={isActive} />
          <span className="flex-1">{tab.label}</span>
          {badge && <CountBadge {...badge} />}
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
            ? "bg-brand-50 text-brand-500"
            : "text-ink-500 hover:bg-ink-400/10 hover:text-ink-900",
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
        inline ? "" : "",
        "inline-flex min-w-[1.15rem] items-center justify-center rounded-full bg-brand-500 px-1",
        "font-mono text-[10px] font-semibold leading-[1.15rem] text-canvas",
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

function DashboardIcon({ active }) {
  return (
    <svg viewBox="0 0 20 20" className="h-4 w-4 shrink-0" fill="none" stroke={active ? "currentColor" : "currentColor"} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="3" width="6" height="6" rx="1" />
      <rect x="11" y="3" width="6" height="6" rx="1" />
      <rect x="3" y="11" width="6" height="6" rx="1" />
      <rect x="11" y="11" width="6" height="6" rx="1" />
    </svg>
  );
}

function ProjectsIcon() {
  return (
    <svg viewBox="0 0 20 20" className="h-4 w-4 shrink-0" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 5a2 2 0 012-2h4l2 2h6a2 2 0 012 2v8a2 2 0 01-2 2H4a2 2 0 01-2-2V5z" />
    </svg>
  );
}

function TriggersIcon() {
  return (
    <svg viewBox="0 0 20 20" className="h-4 w-4 shrink-0" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M11 3L4 12h5l-1 5 7-9h-5l1-5z" />
    </svg>
  );
}

function TemplatesIcon() {
  return (
    <svg viewBox="0 0 20 20" className="h-4 w-4 shrink-0" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="3" width="14" height="14" rx="2" />
      <path d="M3 8h14" />
      <path d="M8 8v9" />
    </svg>
  );
}

function ContentIcon() {
  return (
    <svg viewBox="0 0 20 20" className="h-4 w-4 shrink-0" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M5 3h10a2 2 0 012 2v10a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2z" />
      <path d="M7 7h6M7 10h6M7 13h3" />
    </svg>
  );
}

function CalendarIcon() {
  return (
    <svg viewBox="0 0 20 20" className="h-4 w-4 shrink-0" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="4" width="14" height="13" rx="2" />
      <path d="M7 2v3M13 2v3M3 9h14" />
    </svg>
  );
}

function PublishIcon() {
  return (
    <svg viewBox="0 0 20 20" className="h-4 w-4 shrink-0" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M10 3v10M6 9l4-4 4 4" />
      <path d="M3 14v2a1 1 0 001 1h12a1 1 0 001-1v-2" />
    </svg>
  );
}

function AnalyticsIcon() {
  return (
    <svg viewBox="0 0 20 20" className="h-4 w-4 shrink-0" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 17V10M8 17V7M13 17V3M18 17v-7" />
    </svg>
  );
}

function SettingsIcon() {
  return (
    <svg viewBox="0 0 20 20" className="h-4 w-4 shrink-0" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="10" cy="10" r="3" />
      <path d="M10 1v2M10 17v2M3.5 3.5l1.4 1.4M15.1 15.1l1.4 1.4M1 10h2M17 10h2M3.5 16.5l1.4-1.4M15.1 4.9l1.4-1.4" />
    </svg>
  );
}
