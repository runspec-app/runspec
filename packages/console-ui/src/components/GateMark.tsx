// The runspec gate mark: a run triangle behind a vertical gate bar. The
// console can run tools fast, but only through a gate you control. Shown beside
// the "runspec" wordmark in the sidebar of an unbranded console (a white-label
// brand_name replaces both). Brand rules: docs/design/brand.md.

interface Props {
  size?: number
  /** The gate bar: the primary text colour. */
  barColor: string
  /** The run triangle: the brand accent. */
  triangleColor: string
}

export default function GateMark({ size = 18, barColor, triangleColor }: Props) {
  return (
    <svg width={size} height={size} viewBox="0 0 48 48" aria-hidden="true" data-testid="gate-mark" style={{ flexShrink: 0 }}>
      <path d="M12 9 V39" fill="none" stroke={barColor} strokeWidth={4} strokeLinecap="round" />
      <path d="M20 12 L38 24 L20 36 Z" fill={triangleColor} stroke={triangleColor} strokeWidth={2} strokeLinejoin="round" />
    </svg>
  )
}
