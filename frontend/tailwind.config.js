/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ['"Schibsted Grotesk"', "system-ui", "-apple-system", "sans-serif"],
        mono: ['"IBM Plex Mono"', "ui-monospace", "SFMono-Regular", "monospace"],
        display: ['"Instrument Serif"', "Georgia", "serif"],
      },
      colors: {
        paper: "#1A1A1D",
        canvas: "#0A0A0B",
        ink: {
          900: "#E5E7EB",
          700: "#D1D5DB",
          500: "#9CA3AF",
          400: "#6B7280",
        },
        line: {
          DEFAULT: "#2A2A2D",
          strong: "#333336",
        },
        brand: {
          50: "rgba(240, 180, 41, 0.1)",
          100: "rgba(240, 180, 41, 0.15)",
          300: "#F7CC5F",
          500: "#F0B429",
          600: "#D4A017",
          700: "#8B6914",
        },
        good: "#34D399",
        "good-wash": "rgba(52, 211, 153, 0.1)",
        warn: "#FBBF24",
        "warn-wash": "rgba(251, 191, 36, 0.1)",
        bad: "#F87171",
        "bad-wash": "rgba(248, 113, 113, 0.1)",
      },
      boxShadow: {
        card: "0 1px 2px rgba(0, 0, 0, 0.3), 0 1px 3px rgba(0, 0, 0, 0.2)",
        lift: "0 4px 12px -2px rgba(0, 0, 0, 0.4), 0 2px 4px -2px rgba(0, 0, 0, 0.3)",
        pop: "0 12px 32px -8px rgba(0, 0, 0, 0.5)",
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
