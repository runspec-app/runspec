import type { CSSProperties, ReactNode } from 'react'

/** Lays a Settings tab's sections out side by side (each child is one column),
 *  dropping to a single column when the dialog is too narrow for two. The grid
 *  lives in index.css (.rs-settings-cols). */
export function SettingsColumns({ children, style }: { children: ReactNode; style?: CSSProperties }) {
  return <div className="rs-settings-cols" style={style}>{children}</div>
}
