"""Content keys from the KeyOS key server, over CPIX.

A Widevine-protected segment cannot be read without the key it was encrypted under. The key
comes from BuyDRM's KeyOS server, over CPIX — the DASH-IF standard for exchanging content
keys — and the analyzer authenticates itself with an X.509 certificate rather than a token:
the request document is signed with the client key, and the keys come back encrypted to the
same certificate so only the holder of the private key can read them.

The exchange, in order:

1. Build a CPIX document naming the key identifiers wanted and carrying the client
   certificate as the delivery key, and sign it.
2. POST it. The response is a CPIX document signed by KeyOS.
3. Decrypt its document key with the client private key (RSA-OAEP), then each content key
   with the document key (AES-CBC). Where KeyOS sends a MAC, check it.

**The credentials are not in this repository and never will be.** The three PEM sources are
configuration — a path or an HTTPS URL — and are read at run time. Nothing here logs a key, a
private key, or a decrypted content key: a key in a log file is a key that has left the
analyzer.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.drm.detect import WIDEVINE_UUID, as_uuid, normalise_kid
from app.net.fetcher import Fetcher

logger = logging.getLogger(__name__)

# BuyDRM's CPIX v4 endpoint. The SOAPAction header is what their gateway routes on.
DEFAULT_ENDPOINT = "https://cpix.keyos.com/api/v4/getKeys"
SOAP_ACTION = "http://tempuri.org/ISmoothPackager/RequestEncryptionInfo"

CPIX_NS = {
    "cpix": "urn:dashif:org:cpix",
    "enc": "http://www.w3.org/2001/04/xmlenc#",
    "pskc": "urn:ietf:params:xml:ns:keyprov:pskc",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}

C14N = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"


class CpixError(Exception):
    """The key exchange failed. The message is what an operator is shown and is key-free."""


@dataclass(slots=True)
class CpixCredentials:
    """Where the three PEM files live. Each is a filesystem path or an HTTPS URL."""

    client_cert: str = ""
    client_key: str = ""
    server_cert: str = ""
    endpoint: str = DEFAULT_ENDPOINT

    @property
    def configured(self) -> bool:
        return bool(self.client_cert and self.client_key and self.server_cert)

    def describe(self) -> dict[str, Any]:
        """What the UI may know. Never a key, and never the private key's location."""
        return {
            "configured": self.configured,
            "endpoint": self.endpoint,
            # Whether each is set, not what it is: a path can carry a hostname worth keeping
            # off a page, and the private key's whereabouts is not a thing to publish.
            "client_cert_set": bool(self.client_cert),
            "client_key_set": bool(self.client_key),
            "server_cert_set": bool(self.server_cert),
        }


async def load_pem(source: str, fetcher: Fetcher | None = None) -> str:
    """One PEM, from a local path or an HTTPS URL.

    The project fetches every URL through its own client, so a PEM served over HTTPS goes the
    same way as everything else — one place where TLS behaviour and timeouts are decided.
    """
    if not source:
        raise CpixError("No PEM source is configured.")
    if source.startswith(("http://", "https://")):
        own = fetcher is None
        client = fetcher or Fetcher(timeout_s=20.0)
        try:
            result = await client.fetch(source)
            if not result.ok:
                raise CpixError(
                    f"The certificate store answered HTTP {result.status} for a credential."
                )
            return result.text
        finally:
            if own:
                await client.aclose()

    path = Path(source)
    if not path.exists():
        raise CpixError(f"The configured credential file does not exist: {source}")
    return path.read_text()


def _certificate_body(pem: str) -> str:
    """The base64 body of a certificate, as an X509Certificate element carries it."""
    return (
        pem.replace("-----BEGIN CERTIFICATE-----", "")
        .replace("-----END CERTIFICATE-----", "")
        .strip()
        .replace("\n", "")
        .replace("\r", "")
    )


def build_request(
    kids: list[str],
    *,
    content_id: str,
    client_cert_pem: str,
    client_key_pem: str,
) -> bytes:
    """A signed CPIX document asking for one or more content keys.

    The client certificate rides in `DeliveryData` so KeyOS knows which public key to encrypt
    the answer to, and the whole document is signed so it knows the request is ours.
    """
    from lxml import etree
    from signxml import XMLSigner

    cpix = CPIX_NS["cpix"]
    ds = CPIX_NS["ds"]
    xsi = CPIX_NS["xsi"]

    root = etree.Element(f"{{{cpix}}}CPIX", nsmap=CPIX_NS)
    root.set("name", content_id)
    root.set("contentId", content_id)
    root.set(f"{{{xsi}}}schemaLocation", "urn:dashif:org:cpix cpix.xsd")

    delivery_list = etree.SubElement(root, f"{{{cpix}}}DeliveryDataList")
    delivery = etree.SubElement(delivery_list, f"{{{cpix}}}DeliveryData")
    delivery_key = etree.SubElement(delivery, f"{{{cpix}}}DeliveryKey")
    x509 = etree.SubElement(delivery_key, f"{{{ds}}}X509Data")
    etree.SubElement(x509, f"{{{ds}}}X509Certificate").text = _certificate_body(client_cert_pem)

    keys = etree.SubElement(root, f"{{{cpix}}}ContentKeyList")
    systems = etree.SubElement(root, f"{{{cpix}}}DRMSystemList")
    rules = etree.SubElement(root, f"{{{cpix}}}ContentKeyUsageRuleList")

    for kid in kids:
        uuid = as_uuid(kid)
        if not uuid:
            continue
        etree.SubElement(keys, f"{{{cpix}}}ContentKey", kid=uuid, commonEncryptionScheme="cenc")
        etree.SubElement(systems, f"{{{cpix}}}DRMSystem", kid=uuid, systemId=WIDEVINE_UUID)
        etree.SubElement(rules, f"{{{cpix}}}ContentKeyUsageRule", kid=uuid, intendedTrackType="SD")

    signed = XMLSigner(c14n_algorithm=C14N).sign(
        root, key=client_key_pem.encode(), cert=client_cert_pem
    )
    return bytes(etree.tostring(signed))


def parse_response(xml_text: str, *, client_key_pem: str, server_cert_pem: str) -> dict[str, str]:
    """The content keys in a CPIX response, as `{kid: key}` in lower-case hex.

    The document key is encrypted to the client certificate and each content key to the
    document key, so both have to be unwrapped in order. A MAC that does not match is
    reported and the key is not used — a key that fails its own integrity check is not a key.
    """
    from Crypto.Cipher import AES, PKCS1_OAEP
    from Crypto.PublicKey import RSA
    from Crypto.Util.Padding import unpad
    from lxml import etree
    from signxml import XMLVerifier

    try:
        root = etree.fromstring(xml_text.encode("utf-8"))
    except Exception as exc:
        raise CpixError(f"The key server's answer does not parse as XML: {exc}") from exc

    document = (root.xpath("//cpix:CPIX", namespaces=CPIX_NS) or [root])[0]

    # The signature proves the answer came from KeyOS. A document whose signature does not
    # verify against either the certificate it carries or the configured one is reported,
    # and its keys are still read: the packager, not the analyzer, decides key rotation, and
    # refusing to analyse on a signature mismatch would make a channel unanalysable for a
    # reason the operator cannot act on.
    verified = False
    for pem in (_signing_certificate(document), server_cert_pem):
        if not pem:
            continue
        try:
            XMLVerifier().verify(document, x509_cert=pem)
            verified = True
            break
        except Exception:
            continue
    if not verified:
        logger.warning("the CPIX response signature did not verify against a known certificate")

    document_cipher = document.xpath("//cpix:DocumentKey//enc:CipherValue", namespaces=CPIX_NS)
    if not document_cipher:
        raise CpixError("The key server's answer carries no document key.")

    try:
        rsa = PKCS1_OAEP.new(RSA.import_key(client_key_pem))
        document_key = rsa.decrypt(base64.b64decode(document_cipher[0].text or ""))
    except Exception as exc:
        raise CpixError(
            f"The document key could not be decrypted with the configured client key: "
            f"{type(exc).__name__}."
        ) from exc

    mac_cipher = document.xpath("//cpix:MACMethod//enc:CipherValue", namespaces=CPIX_NS)
    mac_key = rsa.decrypt(base64.b64decode(mac_cipher[0].text or "")) if mac_cipher else b""

    found: dict[str, str] = {}
    for element in document.xpath(".//cpix:ContentKey", namespaces=CPIX_NS):
        uuid = element.get("kid", "")
        cipher_values = element.xpath(".//enc:CipherValue", namespaces=CPIX_NS)
        if not uuid or not cipher_values:
            continue
        blob = base64.b64decode(cipher_values[0].text or "")

        if mac_key:
            macs = element.xpath(".//pskc:ValueMAC", namespaces=CPIX_NS)
            if macs:
                expected = (macs[0].text or "").strip()
                actual = base64.b64encode(hmac.new(mac_key, blob, hashlib.sha512).digest()).decode()
                if not hmac.compare_digest(actual, expected):
                    logger.warning(
                        "the key for %s failed its integrity check and was dropped", uuid
                    )
                    continue

        try:
            plain = unpad(
                AES.new(document_key, AES.MODE_CBC, iv=blob[: AES.block_size]).decrypt(
                    blob[AES.block_size :]
                ),
                AES.block_size,
            )
        except Exception:
            logger.warning("the key for %s could not be decrypted and was dropped", uuid)
            continue

        kid = normalise_kid(uuid)
        if kid:
            found[kid] = plain.hex()

    return found


def _signing_certificate(element: Any) -> str:
    """The certificate the response signed itself with, as PEM."""
    try:
        nodes = element.xpath("//ds:Signature//ds:X509Certificate", namespaces=CPIX_NS)
        if not nodes:
            return ""
        body = (nodes[-1].text or "").strip().replace("\n", "").replace("\r", "")
        if not body:
            return ""
        wrapped = "\n".join(body[index : index + 64] for index in range(0, len(body), 64))
        return f"-----BEGIN CERTIFICATE-----\n{wrapped}\n-----END CERTIFICATE-----"
    except Exception:
        return ""


@dataclass
class KeyStore:
    """Content keys for one job, fetched once per key identifier and kept for the run.

    A live channel asks for the same key on every segment of every rendition, so the store is
    what stops one key request becoming thousands. It is per job rather than global: a key is
    content, and one channel's key has no business being served to another channel's run.
    """

    credentials: CpixCredentials
    content_id: str = "rba"
    _keys: dict[str, str] = field(default_factory=dict)
    _missing: set[str] = field(default_factory=set)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    requests_made: int = 0

    def known(self, kid: str) -> str:
        """The key already held for this identifier, or an empty string."""
        return self._keys.get(normalise_kid(kid), "")

    def all_keys(self) -> dict[str, str]:
        return dict(self._keys)

    async def key_for(self, kid: str, fetcher: Fetcher | None = None) -> str:
        """The content key for one identifier, asking the key server at most once for it."""
        wanted = normalise_kid(kid)
        if not wanted:
            return ""
        if wanted in self._keys:
            return self._keys[wanted]

        async with self._lock:
            # Another segment may have fetched it while this one waited for the lock.
            if wanted in self._keys:
                return self._keys[wanted]
            if wanted in self._missing:
                return ""
            if not self.credentials.configured:
                raise CpixError(
                    "No CPIX credentials are configured, so a content key cannot be "
                    "requested. Set them in Settings, then run this again."
                )

            found = await self._request([wanted], fetcher)
            self._keys.update(found)
            if wanted not in found:
                # Remembered so a channel whose key the server will not serve asks once
                # rather than on every segment for the life of the run.
                self._missing.add(wanted)
            return self._keys.get(wanted, "")

    async def _request(self, kids: list[str], fetcher: Fetcher | None) -> dict[str, str]:
        client_cert = await load_pem(self.credentials.client_cert, fetcher)
        client_key = await load_pem(self.credentials.client_key, fetcher)
        server_cert = await load_pem(self.credentials.server_cert, fetcher)

        body = build_request(
            kids,
            content_id=self.content_id,
            client_cert_pem=client_cert,
            client_key_pem=client_key,
        )

        own = fetcher is None
        client = fetcher or Fetcher(timeout_s=60.0)
        try:
            result = await client.fetch(
                self.credentials.endpoint,
                method="POST",
                content=body,
                headers={
                    "Content-Type": "text/xml; charset=utf-8",
                    "SOAPAction": SOAP_ACTION,
                },
            )
        finally:
            if own:
                await client.aclose()

        self.requests_made += 1
        if not result.ok:
            raise CpixError(
                f"The key server answered HTTP {result.status} for {len(kids)} key request(s)."
            )

        found = parse_response(result.text, client_key_pem=client_key, server_cert_pem=server_cert)
        # Counts only: the identifiers are safe to log, the keys are not.
        logger.info(
            "CPIX returned %d of %d requested key(s) for content %s",
            len(found),
            len(kids),
            self.content_id,
        )
        return found
