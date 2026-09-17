/**
 * Icon set.
 *
 * Drawn inline rather than pulled from a package: the set is small, every glyph inherits
 * `currentColor` so it picks up the surrounding state, and the bundle stays lean.
 */

interface IconProps {
  size?: number
  className?: string
}

function Svg({ size = 18, className, children }: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  )
}

export const IconRealtime = (p: IconProps) => (
  <Svg {...p}>
    <path d="M2 12h4l2.5-7 5 14L16.5 12H22" />
  </Svg>
)

export const IconAging = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5l3.5 2" />
  </Svg>
)

export const IconBulk = (p: IconProps) => (
  <Svg {...p}>
    <path d="M3 7.5 12 3l9 4.5-9 4.5L3 7.5Z" />
    <path d="m3 12.5 9 4.5 9-4.5" />
    <path d="m3 17 9 4.5L21 17" />
  </Svg>
)

export const IconReports = (p: IconProps) => (
  <Svg {...p}>
    <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z" />
    <path d="M14 3v5h5M9 13h6M9 17h4" />
  </Svg>
)

export const IconSettings = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-2.7 1.1v.3a2 2 0 1 1-4 0v-.2a1.6 1.6 0 0 0-2.8-1.1l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.6 1.6 0 0 0 3.5 15H3a2 2 0 1 1 0-4h.2A1.6 1.6 0 0 0 4.3 8.2l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 2.7-1.1V4a2 2 0 1 1 4 0v.2a1.6 1.6 0 0 0 2.8 1.1l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0 1.1 2.7h.3a2 2 0 1 1 0 4h-.2a1.6 1.6 0 0 0-1.4 1Z" />
  </Svg>
)

export const IconCollapse = (p: IconProps) => (
  <Svg {...p}>
    <path d="M15 6 9 12l6 6" />
  </Svg>
)

export const IconExpand = (p: IconProps) => (
  <Svg {...p}>
    <path d="m9 6 6 6-6 6" />
  </Svg>
)

export const IconArrowRight = (p: IconProps) => (
  <Svg {...p}>
    <path d="M5 12h14M13 6l6 6-6 6" />
  </Svg>
)

export const IconArrowLeft = (p: IconProps) => (
  <Svg {...p}>
    <path d="M19 12H5M11 18l-6-6 6-6" />
  </Svg>
)

export const IconUpload = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 16V4M8 8l4-4 4 4" />
    <path d="M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" />
  </Svg>
)

export const IconDownload = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 4v12M8 12l4 4 4-4" />
    <path d="M4 18v1a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-1" />
  </Svg>
)

export const IconSearch = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-3.5-3.5" />
  </Svg>
)

export const IconStop = (p: IconProps) => (
  <Svg {...p}>
    <rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" stroke="none" />
  </Svg>
)

export const IconCheck = (p: IconProps) => (
  <Svg {...p}>
    <path d="m5 12.5 4.5 4.5L19 7" />
  </Svg>
)

export const IconClose = (p: IconProps) => (
  <Svg {...p}>
    <path d="M6 6l12 12M18 6 6 18" />
  </Svg>
)

export const IconFile = (p: IconProps) => (
  <Svg {...p}>
    <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z" />
    <path d="M14 3v5h5" />
  </Svg>
)

export const IconChannels = (p: IconProps) => (
  <Svg {...p}>
    <rect x="2.5" y="7" width="19" height="13" rx="2.5" />
    <path d="m8 3.5 4 3.5 4-3.5" />
    <path d="M10.5 11.5v5l4.5-2.5-4.5-2.5Z" fill="currentColor" stroke="none" />
  </Svg>
)

export const IconGlobe = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M3 12h18" />
    <path d="M12 3a15 15 0 0 1 0 18a15 15 0 0 1 0-18Z" />
  </Svg>
)

export const IconTrash = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 7h16M10 4h4M9 7v12M15 7v12" />
    <path d="M6 7l.8 12.2A2 2 0 0 0 8.8 21h6.4a2 2 0 0 0 2-1.8L18 7" />
  </Svg>
)

export const IconPulse = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="4" fill="currentColor" stroke="none" />
  </Svg>
)

/** A measured trend over time: the field rebuffering metric. */
export const IconTrend = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 19V5" />
    <path d="M4 19h16" />
    <path d="m7 15 4-5 3 3 5-6" />
  </Svg>
)

/**
 * The product mark: the Samsung TV Plus icon, served from `public/` rather than hotlinked,
 * so the rail renders on a deployment with no route to the internet. The same source
 * generates the favicon and the report masthead.
 */
export function BrandMark({ size = 34 }: { size?: number }) {
  return (
    <img
      src="/tvplus-logo.png"
      width={size}
      height={size}
      alt="Samsung TV Plus"
      className="shrink-0 rounded-tile object-cover"
      style={{ width: size, height: size }}
    />
  )
}
