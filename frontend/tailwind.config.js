/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      fontFamily: {
        // A grotesk for UI, mono for anything that is data (statuses, counts,
        // slugs, timestamps), and a serif for the wordmark and big numerals.
        sans: ['"Inter"', "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ['"IBM Plex Mono"', "ui-monospace", "SFMono-Regular", "monospace"],
        display: ['"Instrument Serif"', "Georgia", "serif"],
      },
      colors: {
        // Warm off-white canvas, near-black ink. A clean editorial surface —
        // this is a writing tool, so the page should look like paper.
        paper: "#ffffff",
        canvas: "#fbfaf8",
        ink: {
          900: "#16181d",
          700: "#3d4149",
          500: "#6b7280",
          400: "#9aa1ab",
        },
        line: {
          DEFAULT: "#e8e6e1",
          strong: "#d6d3cc",
        },
        // The single accent: the herald's trumpet. Used only for the thing you
        // should look at or click next.
        brand: {
          50: "#eef4ff",
          100: "#dbe6ff",
          300: "#a3bcff",
          500: "#3b5bdb",
          600: "#2f49b2",
          700: "#25398c",
        },
        good: "#15803d",
        "good-wash": "#e9f6ee",
        warn: "#b45309",
        "warn-wash": "#fdf3e3",
        bad: "#b91c1c",
        "bad-wash": "#fdeded",
      },
      boxShadow: {
        card: "0 1px 2px rgba(22, 24, 29, 0.04), 0 1px 3px rgba(22, 24, 29, 0.03)",
        lift: "0 4px 12px -2px rgba(22, 24, 29, 0.08), 0 2px 4px -2px rgba(22, 24, 29, 0.05)",
        pop: "0 12px 32px -8px rgba(22, 24, 29, 0.18)",
      },
      keyframes: {
        "fade-up": {
          from: { opacity: "0", transform: "translateY(6px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        shimmer: { "100%": { transform: "translateX(100%)" } },
      },
      animation: {
        "fade-up": "fade-up 0.4s cubic-bezier(0.16, 1, 0.3, 1) both",
        shimmer: "shimmer 1.8s infinite",
      },
    },
  },
  plugins: [],
};
