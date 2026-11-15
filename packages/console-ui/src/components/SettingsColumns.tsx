import type { CSSProperties, ReactNode } from 'react'

/** Lays a Settings tab's sections out side by side (each child is one column),
 *  dropping to a single column when the dialog is too narrow for two. The grid
 *  lives in index.css (.rs-settings-cols).
 *
 *  `lastFirstWhenStacked` puts the last column on top once they stack: for a
 *  tab whose last column acts at once (SSH's key panel), so the pinned Save
 *  below the form never floats over it. */
export function SettingsColumns({ children, style, lastFirstWhenStacked }: { children: ReactNode; style?: CSSProperties; lastFirstWhenStacked?: boolean }) {
  if (!lastFirstWhenStacked) return <div className="rs-settings-cols" style={style}>{children}</div>
  return (
    <div className="rs-settings-cols-wrap">
      <div className="rs-settings-cols rs-last-first" style={style}>{children}</div>
    </div>
  )
}
