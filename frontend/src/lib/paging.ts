/**
 * Client-side paging for a list the tab already holds.
 *
 * Analysed channels, reports, aging jobs and bulk batches come back from the backend newest
 * first and capped (200 to 500 rows); a table of all of them is taller than any screen. The
 * page is cut here, after the tab's own search filter, so a search always pages through what
 * it matched and a page never shows rows the filter hid.
 */

import { useEffect, useMemo, useState } from 'react'

export const DEFAULT_PAGE_SIZE = 20

export interface PageSlice<T> {
  rows: T[]
  page: number
  pages: number
  label: string
  hasPrevious: boolean
  hasNext: boolean
}

/** The rows of page `page` (zero-based), clamped to the pages that exist. */
export function pageOf<T>(items: T[], page: number, size = DEFAULT_PAGE_SIZE): PageSlice<T> {
  const pages = Math.max(1, Math.ceil(items.length / size))
  const current = Math.min(Math.max(0, page), pages - 1)
  const start = current * size
  const rows = items.slice(start, start + size)
  const label =
    items.length === 0
      ? 'No rows'
      : `${start + 1}–${start + rows.length} of ${items.length} · page ${current + 1} of ${pages}`
  return {
    rows,
    page: current,
    pages,
    label,
    hasPrevious: current > 0,
    hasNext: current < pages - 1,
  }
}

/**
 * Paging state for one table. `resetKey` is whatever narrows the list — the search text — so
 * a new search starts on its first page rather than on a page that no longer exists.
 */
export function usePaged<T>(items: T[], resetKey = '', size = DEFAULT_PAGE_SIZE) {
  const [page, setPage] = useState(0)
  useEffect(() => setPage(0), [resetKey])
  const slice = useMemo(() => pageOf(items, page, size), [items, page, size])
  return {
    ...slice,
    previous: () => setPage(slice.page - 1),
    next: () => setPage(slice.page + 1),
  }
}
