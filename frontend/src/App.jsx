import { Navigate, Route, Routes } from "react-router-dom";
import { ErrorFallback, RouteErrorBoundary } from "./components/ErrorBoundary";
import Shell from "./components/Shell";
import { useAuth } from "./hooks/useAuth";
import Analytics from "./pages/Analytics";
import Calendar from "./pages/Calendar";
import ContentEditor from "./pages/ContentEditor";
import ContentList from "./pages/ContentList";
import Dashboard from "./pages/Dashboard";
import LandingPage from "./pages/LandingPage";
import Login from "./pages/Login";
import PreviewPage from "./pages/PreviewPage";
import Projects from "./pages/Projects";
import Publish from "./pages/Publish";
import Settings from "./pages/Settings";
import Templates from "./pages/Templates";
import Triggers from "./pages/Triggers";

function Loading() {
  return (
    <div className="flex min-h-screen items-center justify-center">
      <span className="eyebrow animate-pulse">Loading</span>
    </div>
  );
}

/**
 * Sign-in gate, chrome, and the boundary between the two.
 *
 * The boundary sits *inside* `Shell`, so a page that throws leaves the
 * navigation, the account menu and the queue badges standing — the user can
 * click their way out of the broken screen instead of reloading a blank tab.
 * It resets on navigation, so clicking away is all it takes.
 */
function Protected({ children }) {
  const { user, loading } = useAuth();
  if (loading) return <Loading />;
  if (!user) return <Navigate to="/" replace />;
  return (
    <Shell>
      <RouteErrorBoundary title="This page stopped working" section="page">
        {children}
      </RouteErrorBoundary>
    </Shell>
  );
}

/**
 * The boundary for a route that renders without the Shell.
 *
 * Same containment, different framing: with no chrome around it, the default
 * panel would sit alone against the top of an otherwise empty page, so these
 * two centre it the way their own content is centred.
 */
function Unshelled({ children, section }) {
  return (
    <RouteErrorBoundary
      section={section}
      fallback={({ error, reset }) => (
        <div className="flex min-h-screen items-center justify-center px-5">
          <div className="w-full max-w-md">
            <ErrorFallback error={error} reset={reset} />
          </div>
        </div>
      )}
    >
      {children}
    </RouteErrorBoundary>
  );
}

export default function App() {
  return (
    <Routes>
      <Route
        path="/"
        element={
          <Unshelled section="landing">
            <LandingPage />
          </Unshelled>
        }
      />
      <Route
        path="/login"
        element={
          <Unshelled section="login">
            <Login />
          </Unshelled>
        }
      />
      {/* Unauthenticated on purpose — see app.services.preview_links. */}
      <Route
        path="/preview/:token"
        element={
          <Unshelled section="preview">
            <PreviewPage />
          </Unshelled>
        }
      />

      <Route
        path="/dashboard"
        element={
          <Protected>
            <Dashboard />
          </Protected>
        }
      />
      <Route
        path="/projects"
        element={
          <Protected>
            <Projects />
          </Protected>
        }
      />
      <Route
        path="/content"
        element={
          <Protected>
            <ContentList />
          </Protected>
        }
      />
      {/* The editor is entered from a piece, not from the nav. */}
      <Route
        path="/content/:contentId"
        element={
          <Protected>
            <ContentEditor />
          </Protected>
        }
      />
      <Route
        path="/triggers"
        element={
          <Protected>
            <Triggers />
          </Protected>
        }
      />
      <Route
        path="/templates"
        element={
          <Protected>
            <Templates />
          </Protected>
        }
      />
      <Route
        path="/calendar"
        element={
          <Protected>
            <Calendar />
          </Protected>
        }
      />
      <Route
        path="/publish"
        element={
          <Protected>
            <Publish />
          </Protected>
        }
      />
      <Route
        path="/analytics"
        element={
          <Protected>
            <Analytics />
          </Protected>
        }
      />
      <Route
        path="/settings"
        element={
          <Protected>
            <Settings />
          </Protected>
        }
      />

      <Route path="*" element={<Navigate to="/dashboard" replace />} />
    </Routes>
  );
}
