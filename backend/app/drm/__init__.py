"""Content protection: what a stream is protected with, and how to read it anyway.

A DRM-protected channel rebuffers the same way a clear one does, and the operator analysing
it should not have to do anything different. Nothing about DRM is asked per channel: a
protected playback URL goes into Realtime, Aging, Bulk or an automated batch exactly like a
clear one, and the backend works the rest out.

Two separate problems sit behind that.

**Playing it.** The browser player needs a Widevine licence, and it cannot ask for one
itself: the licence server sends no CORS headers and the deployment host is the one with a
route to it. `POST /api/drm/license` relays the challenge, so the page is configured with
this application's own path and never sees the licence server's address.

**Analysing it.** The segments come off the CDN encrypted, so the bitstream rules — decode
errors, SPS consistency, ADTS configuration, keyframe detection — have nothing to read.
`detect.py` says what the channel is protected with and which key it asks for, `cpix.py`
obtains that key from the KeyOS key server, `cenc.py` undoes the encryption in this process,
and `context.py` holds the state for one run so the key server is asked once per key
identifier however many segments and renditions want it. Nothing downstream changes: the
segment reaches `media.analyse()` as plaintext and every rule runs as it always did.

What never happens silently: a rendition whose key could not be obtained is reported as one,
through `INFO-001` and in the report's own protection table, with the reason it was not read —
never analysed as though the bitstream checks had passed.

No key material lives in this package or in this repository. The CPIX credentials are a
location — a path on the analyzer host or an HTTPS URL — configured once in Settings → DRM or
in `backend/.env`, and nothing here logs a key, a private key, or a decrypted content key.
"""

from app.drm.cenc import TrackEncryption, decrypt_fragment, read_track_encryption
from app.drm.context import DrmContext, RenditionProtection
from app.drm.decrypt import DecryptResult, decrypt_segment
from app.drm.detect import DrmInfo, KeySystem, describe_keys, extract_kid, key_system

__all__ = [
    "DecryptResult",
    "DrmContext",
    "DrmInfo",
    "KeySystem",
    "RenditionProtection",
    "TrackEncryption",
    "decrypt_fragment",
    "decrypt_segment",
    "describe_keys",
    "extract_kid",
    "key_system",
    "read_track_encryption",
]
