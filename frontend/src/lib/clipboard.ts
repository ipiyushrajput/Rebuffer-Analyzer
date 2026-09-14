/**
 * Copying to the clipboard.
 *
 * `navigator.clipboard` exists only in a secure context — HTTPS, or localhost. The analyzer
 * is served over plain HTTP on the deployment host, so the async API is simply absent there
 * and every copy button silently rejected. The legacy `execCommand` path has no such
 * restriction, so it is the fallback rather than the exception.
 */

/** Copy `text`, returning whether it reached the clipboard. */
export async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      /* a denied permission or an insecure context falls through to the selection path */
    }
  }
  return legacyCopy(text)
}

/**
 * The pre-Clipboard-API route: put the text in an off-screen textarea, select it, and let
 * the document copy the selection. Works on plain HTTP in every browser the operators use.
 */
function legacyCopy(text: string): boolean {
  const area = document.createElement('textarea')
  area.value = text
  // Off-screen rather than hidden: a display:none element cannot hold a selection.
  area.setAttribute('readonly', '')
  area.style.position = 'fixed'
  area.style.top = '-1000px'
  area.style.opacity = '0'
  document.body.appendChild(area)

  try {
    area.select()
    area.setSelectionRange(0, area.value.length)
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    document.body.removeChild(area)
  }
}
