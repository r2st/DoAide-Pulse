import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.js"],
    coverage: {
      provider: "v8",
      // `src` only. The two config files at the root are build inputs Vite
      // reads and no test imports, so counting them dragged the headline
      // figure down by a point and a half for no coverable line.
      include: ["src/**/*.{js,jsx}"],
      // The test helpers cover themselves by being used; reporting on them
      // says nothing about the app.
      exclude: ["src/**/*.test.{js,jsx}", "src/test/**"],
      reporter: ["text", "html"],
      // `src` is at 100% statements, lines and functions as of this commit.
      // The gate sits at 99 rather than 100 so that adding a file with one
      // genuinely unreachable line is a conversation and not a red build —
      // but it is close enough that a whole untested component cannot arrive
      // quietly, which is what having no gate at all allowed.
      //
      // Branches sit lower on purpose, at a point just under the 98.3% the
      // suite reaches. What is left are defensive arms the render gating above
      // them makes unreachable — a guard inside a handler whose own button is
      // already disabled, a `?? 2` for a severity nothing constructs, a `??  []`
      // on a prop the parent only passes once its own fetch has resolved.
      // Demanding 100% there would mean deleting safety to satisfy a number.
      //
      // Ratcheted 96 -> 98 once the reachable ones were written. The slack is
      // deliberately thin: a gate two points below where the suite sits is a
      // gate that lets two points of real coverage rot away before it says so.
      thresholds: {
        statements: 99,
        lines: 99,
        functions: 99,
        branches: 98,
      },
    },
  },
});
