/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ['"Schibsted Grotesk"', "system-ui", "-apple-system", "sans-serif"],
        mono: ['"IBM Plex Mono"', "ui-monospace", "SFMono-Regular", "monospace"],
        display: ['"Instrument Serif"', "Georgia", "serif"],
      },
      colors: {
        canvas: "rgb(var(--c-canvas) / <alpha-value>)",
        paper: "rgb(var(--c-paper) / <alpha-value>)",
        ink: {
          900: "rgb(var(--c-ink-900) / <alpha-value>)",
          700: "rgb(var(--c-ink-700) / <alpha-value>)",
          500: "rgb(var(--c-ink-500) / <alpha-value>)",
          400: "rgb(var(--c-ink-400) / <alpha-value>)",
        },
        line: {
          DEFAULT: "rgb(var(--c-line) / <alpha-value>)",
          strong: "rgb(var(--c-line-strong) / <alpha-value>)",
        },
        brand: {
          50: "rgba(240, 180, 41, 0.1)",
          100: "rgba(240, 180, 41, 0.15)",
          300: "rgb(var(--c-brand-300) / <alpha-value>)",
          500: "rgb(var(--c-brand-500) / <alpha-value>)",
          600: "rgb(var(--c-brand-600) / <alpha-value>)",
          700: "rgb(var(--c-brand-700) / <alpha-value>)",
        },
        good: "#34D399",
        "good-wash": "rgba(52, 211, 153, 0.1)",
        warn: "#FBBF24",
        "warn-wash": "rgba(251, 191, 36, 0.1)",
        bad: "#F87171",
        "bad-wash": "rgba(248, 113, 113, 0.1)",
      },
      boxShadow: {
        card: "var(--shadow-card)",
        lift: "var(--shadow-lift)",
        pop: "var(--shadow-pop)",
        gold: "0 0 20px rgba(240, 180, 41, 0.15)",
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
