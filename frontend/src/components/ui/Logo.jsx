/** The Herald mark: a trumpet with three sound arcs. */
export default function Logo({ className = "h-6 w-6", title }) {
  return (
    <svg
      viewBox="0 0 32 32"
      className={className}
      role={title ? "img" : undefined}
      aria-hidden={title ? undefined : "true"}
      fill="none"
    >
      {title && <title>{title}</title>}
      <path d="M6 16 L19 9.5 L19 22.5 Z" fill="currentColor" />
      <path
        d="M22 12.5a5 5 0 0 1 0 7M25.5 10a9 9 0 0 1 0 12"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        opacity="0.45"
      />
    </svg>
  );
}
