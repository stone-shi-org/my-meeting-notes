import type { Config } from 'tailwindcss';

/**
 * The design token manifest. Every colour is a CSS variable defined in
 * src/styles/globals.css so light and dark swap in one place; nothing in the
 * app should ever write a raw hex.
 */
export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: 'var(--bg)',
        surface: {
          DEFAULT: 'var(--surface)',
          2: 'var(--surface-2)',
          3: 'var(--surface-3)',
        },
        overlay: 'var(--overlay)',
        border: {
          DEFAULT: 'var(--border)',
          strong: 'var(--border-strong)',
        },
        fg: {
          DEFAULT: 'var(--fg)',
          muted: 'var(--fg-muted)',
          subtle: 'var(--fg-subtle)',
          // Decorative only -- fails AA for text by design. Icons, rails, placeholders.
          faint: 'var(--fg-faint)',
        },
        primary: {
          DEFAULT: 'var(--primary)',
          hover: 'var(--primary-hover)',
          active: 'var(--primary-active)',
          fg: 'var(--primary-fg)',
          soft: 'var(--primary-soft)',
          'soft-fg': 'var(--primary-soft-fg)',
        },
        // `mark` variants are for borders/dots/bars; `ink` variants are the only
        // ones that may carry text.
        success: {
          DEFAULT: 'var(--success)',
          ink: 'var(--success-ink)',
          soft: 'var(--success-soft)',
        },
        warning: {
          DEFAULT: 'var(--warning)',
          ink: 'var(--warning-ink)',
          soft: 'var(--warning-soft)',
        },
        danger: {
          DEFAULT: 'var(--danger)',
          ink: 'var(--danger-ink)',
          soft: 'var(--danger-soft)',
        },
        info: {
          DEFAULT: 'var(--info)',
          ink: 'var(--info-ink)',
          soft: 'var(--info-soft)',
        },
        entity: {
          meeting: 'var(--entity-meeting)',
          event: 'var(--entity-event)',
          email: 'var(--entity-email)',
        },
      },

      fontFamily: {
        sans: ['"Inter Variable"', 'Inter', 'system-ui', 'sans-serif'],
        display: ['Outfit', '"Inter Variable"', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono Variable"', 'ui-monospace', 'monospace'],
      },

      fontSize: {
        '2xs': ['0.6875rem', { lineHeight: '1rem', letterSpacing: '0.01em' }],
        xs: ['0.75rem', { lineHeight: '1.125rem', letterSpacing: '0.005em' }],
        sm: ['0.8125rem', { lineHeight: '1.25rem' }],
        base: ['0.875rem', { lineHeight: '1.5rem' }],
        // Transcript body: deliberately larger and looser than the surrounding
        // chrome, because it is the one thing people actually read.
        md: ['0.9375rem', { lineHeight: '1.6875rem' }],
        lg: ['1.0625rem', { lineHeight: '1.625rem' }],
        xl: ['1.25rem', { lineHeight: '1.75rem', letterSpacing: '-0.01em' }],
        '2xl': ['1.5rem', { lineHeight: '1.875rem', letterSpacing: '-0.015em' }],
        '3xl': ['1.875rem', { lineHeight: '2.25rem', letterSpacing: '-0.02em' }],
        '4xl': ['2.5rem', { lineHeight: '2.75rem', letterSpacing: '-0.025em' }],
      },

      spacing: {
        '4.5': '1.125rem',
        '13': '3.25rem',
        '18': '4.5rem',
        '112': '28rem',
        '128': '32rem',
      },

      borderRadius: {
        sm: '0.375rem',
        DEFAULT: '0.5rem',
        md: '0.625rem',
        lg: '0.75rem',
        xl: '1rem',
        '2xl': '1.25rem',
      },

      boxShadow: {
        // Two-layer ramp: a tight contact shadow plus a soft ambient one. A
        // single-layer shadow is the clearest tell of an undesigned UI.
        xs: '0 1px 2px -1px hsl(var(--shadow-color) / 0.08)',
        sm: '0 1px 2px -1px hsl(var(--shadow-color) / 0.08), 0 2px 6px -2px hsl(var(--shadow-color) / 0.06)',
        md: '0 2px 4px -2px hsl(var(--shadow-color) / 0.10), 0 6px 16px -4px hsl(var(--shadow-color) / 0.08)',
        lg: '0 4px 8px -4px hsl(var(--shadow-color) / 0.12), 0 16px 32px -8px hsl(var(--shadow-color) / 0.10)',
        xl: '0 8px 16px -8px hsl(var(--shadow-color) / 0.14), 0 32px 64px -16px hsl(var(--shadow-color) / 0.14)',
        focus: '0 0 0 3px var(--ring)',
      },

      transitionTimingFunction: {
        out: 'cubic-bezier(0.16, 1, 0.3, 1)',
        inout: 'cubic-bezier(0.65, 0, 0.35, 1)',
        spring: 'cubic-bezier(0.34, 1.56, 0.64, 1)',
      },

      transitionDuration: {
        fast: '120ms',
        DEFAULT: '180ms',
        slow: '280ms',
        slower: '420ms',
      },

      screens: {
        '2xl': '1600px',
        '3xl': '1920px',
      },

      keyframes: {
        'fade-in': { from: { opacity: '0' }, to: { opacity: '1' } },
        'zoom-in': {
          from: { opacity: '0', transform: 'scale(0.95)' },
          to: { opacity: '1', transform: 'scale(1)' },
        },
        shimmer: {
          '100%': { transform: 'translateX(100%)' },
        },
        glow: {
          '0%, 100%': {
            boxShadow:
              '0 0 0 0 var(--ring), 0 4px 8px -4px hsl(var(--shadow-color) / 0.12), 0 16px 32px -8px hsl(var(--shadow-color) / 0.10)',
          },
          '50%': {
            boxShadow:
              '0 0 16px 4px var(--ring), 0 4px 8px -4px hsl(var(--shadow-color) / 0.12), 0 16px 32px -8px hsl(var(--shadow-color) / 0.10)',
          },
        },
      },
      animation: {
        'fade-in': 'fade-in 180ms cubic-bezier(0.16, 1, 0.3, 1)',
        'zoom-in': 'zoom-in 180ms cubic-bezier(0.16, 1, 0.3, 1)',
        glow: 'glow 2.4s ease-in-out infinite',
      },
    },
  },
  plugins: [require('tailwindcss-animate'), require('@tailwindcss/typography')],
} satisfies Config;
