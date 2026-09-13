/**
 * Signal Clarity design system.
 *
 * Colour behaves like telemetry rather than decoration: Samsung blue orients and acts,
 * TV Plus pink marks intervention, and the blue → purple → pink spectrum appears only at
 * moments of high significance. No colour outside this system is introduced.
 *
 * @type {import('tailwindcss').Config}
 */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        // One UI Sans and SamsungOne are used where licensed; Inter is the web fallback.
        sans: ['"One UI Sans"', 'SamsungOne', 'Inter', 'Arial', 'sans-serif'],
        mono: ['"JetBrains Mono"', '"Roboto Mono"', 'ui-monospace', 'monospace'],
      },
      fontSize: {
        // The scale the brief specifies, so every screen reads at the same rhythm.
        label: ['11px', { lineHeight: '16px', letterSpacing: '0.06em', fontWeight: '600' }],
        micro: ['12px', { lineHeight: '16px' }],
        small: ['13px', { lineHeight: '20px' }],
        body: ['14px', { lineHeight: '22px' }],
        card: ['16px', { lineHeight: '24px', fontWeight: '600' }],
        section: ['20px', { lineHeight: '28px', fontWeight: '600' }],
        page: ['28px', { lineHeight: '36px', fontWeight: '600', letterSpacing: '-0.01em' }],
        metric: ['26px', { lineHeight: '32px', fontWeight: '600', letterSpacing: '-0.01em' }],
        metricLg: ['34px', { lineHeight: '40px', fontWeight: '600', letterSpacing: '-0.02em' }],
      },
      colors: {
        brand: {
          50: '#EEF1FC',
          100: '#DDE2F7',
          200: '#B9C2EC',
          300: '#8D9BDE',
          400: '#5568C4',
          500: '#2B41AE',
          600: '#1428A0', // Samsung blue — primary actions and navigation state
          700: '#101F80',
          800: '#0C1760',
          900: '#081040',
        },
        violet: {
          50: '#F5ECFD',
          100: '#EBDBFB',
          200: '#D7BBF5',
          300: '#B681EA',
          500: '#7B2CBF', // gradient midpoint — warnings and secondary emphasis
          600: '#66229F',
          700: '#4E1A7A',
        },
        pink: {
          50: '#FFECF1',
          100: '#FFD9E2',
          200: '#FFB3C4',
          300: '#FF7E9B',
          500: '#FF2D55', // TV Plus pink — intervention, escalation, exceptional states
          600: '#E01142',
          700: '#B00B33',
        },
        clean: {
          50: '#E7F7EE',
          100: '#CBEEDB',
          500: '#12864C',
          600: '#0F7B45',
        },
        rail: {
          DEFAULT: '#0D1430', // navigation rail — the operator's fixed anchor
          hover: '#16204A',
          active: '#1B2755',
          border: '#222C55',
          muted: '#9AA4C4',
          text: '#E7EAF5',
        },
        ink: {
          DEFAULT: '#0B1020',
          soft: '#31384D',
          muted: '#667085',
          faint: '#98A0B4',
        },
        surface: {
          DEFAULT: '#FFFFFF',
          sunken: '#F4F5F9', // page ground
          raised: '#FAFBFD',
          line: '#E6E8F0',
          lineStrong: '#D4D8E6',
        },
      },
      boxShadow: {
        card: '0 1px 2px rgba(11, 16, 32, 0.04)',
        raised: '0 2px 8px rgba(11, 16, 32, 0.06), 0 1px 2px rgba(11, 16, 32, 0.04)',
        rail: '1px 0 0 rgba(34, 44, 85, 0.6)',
        pop: '0 12px 32px rgba(11, 16, 32, 0.12)',
      },
      borderRadius: {
        card: '10px',
        tile: '8px',
        pill: '999px',
      },
      backgroundImage: {
        // Reserved for moments of high significance, so the spectrum keeps its impact.
        spectrum: 'linear-gradient(100deg, #1428A0 0%, #7B2CBF 58%, #FF2D55 100%)',
        'spectrum-soft': 'linear-gradient(100deg, #1428A0 0%, #7B2CBF 100%)',
      },
      transitionDuration: { 150: '150ms' },
    },
  },
  plugins: [],
}
