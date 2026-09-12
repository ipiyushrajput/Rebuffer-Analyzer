/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['"Space Grotesk"', 'system-ui', '-apple-system', 'sans-serif'],
        mono: ['"IBM Plex Mono"', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },
      colors: {
        // Samsung TV Plus blue, carried from the Metanalyser design.
        brand: {
          50: '#eef2ff', 100: '#e0e7ff', 200: '#c7d2fe', 300: '#a5b4fc',
          400: '#818cf8', 500: '#4f60d8', 600: '#1428A0', 700: '#101f80',
          800: '#0c1760', 900: '#081040',
        },
        // Severity colours are paired with an icon and a label so they never carry meaning alone.
        critical: '#b4151b',
        error: '#d4581a',
        warn: '#9a6700',
        info: '#1f6feb',
        pass: '#1a7f37',
      },
      boxShadow: {
        card: '0 1px 2px rgba(16,24,40,0.06), 0 1px 3px rgba(16,24,40,0.10)',
      },
    },
  },
  plugins: [],
}
