/**
 * How a list of read hosts is named, the same everywhere: local plus the
 * connected hosts of the active profile ("local + 2 connected"), so a "3"
 * here never looks like it disagrees with the sidebar's 6 or the footer's
 * "2/5 remote hosts connected".
 */
export function readHostsLabel(count: number): string {
  if (count <= 1) return 'local only'
  return `local + ${count - 1} connected`
}
