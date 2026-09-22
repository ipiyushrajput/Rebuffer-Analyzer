/**
 * The User-Agent profiles, read from the backend.
 *
 * There is one list and the backend owns it: it is the side that makes the requests, and a
 * second copy in the client drifted from it — five entries with labels that no longer
 * matched the strings being sent. The full string is served alongside the label so a form
 * can show exactly what a run will identify as.
 */

import { useQuery } from '@tanstack/react-query'
import { endpoints } from '../api/client'

export interface UaProfile {
  id: string
  label: string
  user_agent: string
}

/** Every profile the backend offers, in the order it lists them (newest Tizen first). */
export function useUaProfiles(): UaProfile[] {
  const { data } = useQuery({
    queryKey: ['settings'],
    queryFn: endpoints.settings,
    // The list changes when the backend is redeployed, not while a tab is open.
    staleTime: Infinity,
  })
  return (data as { ua_profiles?: UaProfile[] } | undefined)?.ua_profiles ?? []
}
