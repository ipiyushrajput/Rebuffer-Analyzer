"""Content protection: what a stream is protected with, and how to read it anyway.

A DRM-protected channel rebuffers the same way a clear one does, and the operator analysing
it should not have to do anything different. Two separate problems sit behind that:

**Playing it.** The browser player needs a licence, which `app/drm/license.py` fetches on the
backend so the licence URL and any credential stay server-side.

**Analysing it.** The segments come off the CDN encrypted, so the bitstream rules — decode
errors, SPS consistency, ADTS configuration — have nothing to read. `app/drm/cpix.py` asks
the KeyOS key server for the content key and `app/drm/decrypt.py` uses it to turn the
payload back into something the existing rules can judge. Nothing downstream changes: the
segment reaches `media.analyse()` as plaintext and every rule runs as it always did.

What never happens silently: a channel whose key could not be obtained is reported as one,
through `INFO-001`, rather than analysed as if the bitstream checks had passed.
"""

from app.drm.detect import DrmInfo, KeySystem, describe_keys, extract_kid, key_system

__all__ = ["DrmInfo", "KeySystem", "describe_keys", "extract_kid", "key_system"]
