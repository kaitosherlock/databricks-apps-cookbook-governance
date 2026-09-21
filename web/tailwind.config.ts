import type { Config } from "tailwindcss";

/**
 * Colours are declared once as CSS variables in globals.css and referenced
 * here, so light and dark are the same token set with different values. No
 * component ever writes a raw hex.
 */
const config: Config = {
  darkMode: "class",
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "var(--bg)",
        surface: "var(--surface)",
        raised: "var(--raised)",
        border: "var(--border)",
        "border-strong": "var(--border-strong)",
        ink: "var(--ink)",
        muted: "var(--ink-muted)",
        faint: "var(--ink-faint)",
        primary: { DEFAULT: "var(--primary)", hover: "var(--primary-hover)", ink: "var(--primary-ink)", soft: "var(--primary-soft)" },
        danger: { DEFAULT: "var(--danger)", ink: "var(--danger-ink)", soft: "var(--danger-soft)", border: "var(--danger-border)" },
        caution: { DEFAULT: "var(--caution)", ink: "var(--caution-ink)", soft: "var(--caution-soft)", border: "var(--caution-border)" },
        info: { DEFAULT: "var(--info)", ink: "var(--info-ink)", soft: "var(--info-soft)", border: "var(--info-border)" },
        good: { DEFAULT: "var(--good)", ink: "var(--good-ink)", soft: "var(--good-soft)", border: "var(--good-border)" },
      },
      fontFamily: {
        // Be Vietnam Pro is drawn for Vietnamese: this app is entirely in
        // Vietnamese and stacked diacritics (ế ự ỗ ằ) have to sit correctly at
        // 13-14px in dense tables. IBM Plex Mono carries the identifiers -
        // full names, privilege codes, event ids - which are everywhere here.
        sans: ["var(--font-sans)", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "monospace"],
      },
      fontSize: {
        "2xs": ["0.6875rem", { lineHeight: "1rem" }],
      },
      borderRadius: {
        sm: "var(--radius-sm)",
        DEFAULT: "var(--radius)",
        md: "var(--radius)",
        lg: "var(--radius-lg)",
      },
      boxShadow: {
        card: "var(--shadow-card)",
        pop: "var(--shadow-pop)",
      },
      keyframes: {
        "fade-in": { from: { opacity: "0" }, to: { opacity: "1" } },
        shimmer: { "100%": { transform: "translateX(100%)" } },
      },
      animation: {
        "fade-in": "fade-in 120ms ease-out",
        shimmer: "shimmer 1.4s infinite",
      },
    },
  },
  plugins: [],
};

export default config;
