import { Component } from "react";
import { useLocation } from "react-router-dom";

/**
 * Catch a render-time throw and put something recoverable in its place.
 *
 *   <ErrorBoundary section="analytics">
 *     <Charts />
 *   </ErrorBoundary>
 *
 * Without one of these, a single bad field in an API response takes the whole
 * document node with it: React unmounts the entire tree from the root and the
 * user is left on a blank white page with no navigation and nothing to click.
 * That is the failure this exists to prevent — not to make the error go away,
 * but to keep it contained to the part that broke.
 *
 * A class, because `componentDidCatch` has no hook equivalent in React 18;
 * boundaries are the one place function components still cannot reach.
 *
 * Note that a boundary only catches throws from *rendering*, from lifecycle
 * methods, and from constructors below it. A throw inside an event handler or
 * an unawaited promise never passes through React's render path, so it does not
 * arrive here — those are `useApi`'s `error` and `ErrorBanner`'s job, and the
 * two mechanisms are complements rather than alternatives.
 *
 * @param {object} props
 * @param {import("react").ReactNode} props.children The subtree to guard.
 * @param {string} [props.title] Heading for the fallback.
 * @param {string} [props.section] Named in the `onError` report, so a handler
 *   can say *which* boundary tripped without a stack to read.
 * @param {(error: Error, info: {componentStack: string}, section?: string) => void} [props.onError]
 *   Called once per caught error, after the fallback is committed.
 * @param {(state: {error: Error, reset: () => void}) => import("react").ReactNode} [props.fallback]
 *   Replaces the default fallback. Use it where the default panel would be the
 *   wrong shape — a broken table row, say, rather than a whole section.
 * @param {unknown} [props.resetKey] Clears a caught error whenever it changes.
 *   `RouteErrorBoundary` passes the pathname, so navigating away from a page
 *   that threw is enough to recover it.
 */
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
    this.reset = this.reset.bind(this);
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // React has already written the error and the component stack to
    // `console.error` by the time this runs, so this is for a caller who wants
    // to do something more than that — not for logging it a second time.
    if (this.props.onError) this.props.onError(error, info, this.props.section);
  }

  componentDidUpdate(prevProps) {
    if (this.state.error !== null && prevProps.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  reset() {
    this.setState({ error: null });
  }

  render() {
    const { error } = this.state;
    if (error === null) return this.props.children;
    if (this.props.fallback) return this.props.fallback({ error, reset: this.reset });
    return (
      <ErrorFallback error={error} reset={this.reset} title={this.props.title} />
    );
  }
}

/**
 * The default fallback: says what happened, keeps the detail, offers the retry.
 *
 * "Try again" only re-renders the children — it cannot fix a throw that is
 * deterministic in the data, and in that case the boundary simply catches the
 * same error again. It is there for the transient half: a render that raced a
 * state update, a chart handed a list that was briefly empty.
 */
export function ErrorFallback({ error, reset, title = "This section could not load" }) {
  return (
    <div
      role="alert"
      className="panel flex flex-col items-center gap-3 px-6 py-12 text-center"
    >
      <p className="text-sm font-medium text-ink-900">{title}</p>
      <p className="max-w-sm text-sm text-ink-500">
        This section stopped rendering. The rest of the page is unaffected —
        try again, or reload if it keeps happening.
      </p>
      {/* The raw message, because the person hitting this is usually the person
          who can act on it, and "an error occurred" is not a bug report. */}
      {error?.message && (
        <p className="max-w-full break-words font-mono text-xs text-ink-400">
          {error.message}
        </p>
      )}
      {reset && (
        <button type="button" className="btn-ghost mt-1" onClick={reset}>
          Try again
        </button>
      )}
    </div>
  );
}

/**
 * A boundary around one panel inside a page.
 *
 * The mechanism is identical to a route-level boundary; only the wording
 * differs, and it differs for a reason. At this scale the user has lost a panel
 * and still has the page, so a fallback saying the page stopped working would
 * overstate what happened and send them reloading for no reason. Kept here
 * rather than written out at each call site so every panel says the same thing.
 *
 * Worth reaching for wherever a panel loads its own data or derives its own
 * numbers — those are the two places a shape the API never promised turns into
 * a throw, and a sidebar panel is not worth an unsaved draft.
 *
 * @param {object} props
 * @param {string} props.name Identifies the panel in an `onError` report.
 * @param {string} [props.title]
 * @param {import("react").ReactNode} props.children
 */
export function SectionBoundary({
  name,
  title = "This panel could not be drawn",
  children,
}) {
  return (
    <ErrorBoundary section={name} title={title}>
      {children}
    </ErrorBoundary>
  );
}

/**
 * An `ErrorBoundary` that forgets its error when the route changes.
 *
 * A boundary with no reset is a trap: it holds the fallback for as long as it
 * stays mounted, so one thrown render on the dashboard would follow the user
 * to every other page that shares the boundary. Keying on the pathname makes
 * navigation the escape hatch, which is what a user tries first anyway.
 */
export function RouteErrorBoundary({ children, ...rest }) {
  const { pathname } = useLocation();
  return (
    <ErrorBoundary resetKey={pathname} {...rest}>
      {children}
    </ErrorBoundary>
  );
}
