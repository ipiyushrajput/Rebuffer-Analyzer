"""The content-key exchange with the KeyOS key server.

The request is signed with the analyzer's client certificate and the keys come back encrypted
to it, so nothing about this exchange can be checked by reading the XML alone: a document that
looks right and is signed wrong is refused by the server, and a key unwrapped in the wrong
order is silently the wrong key.

So the whole round trip is exercised here against a certificate and key generated for the
test. A stand-in key server does exactly what KeyOS does — verify the signature, wrap a
document key to the client certificate, wrap each content key under the document key, and MAC
it — and the test asserts that what comes back out is the key that went in.

No real credential and no real key appears in this file or anywhere in the repository.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac

import pytest

from app.drm.cpix import (
    CPIX_NS,
    CpixCredentials,
    CpixError,
    KeyStore,
    build_request,
    parse_response,
)
from app.drm.detect import as_uuid

KID = "0123456789abcdef0123456789abcdef"
OTHER_KID = "fedcba9876543210fedcba9876543210"
CONTENT_KEY = "00112233445566778899aabbccddeeff"


# -- a certificate and key, made here and used nowhere else -------------------


@pytest.fixture(scope="module")
def credentials() -> tuple[str, str]:
    """A self-signed client certificate and its private key, as PEM."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "RBA CPIX test")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return cert.public_bytes(serialization.Encoding.PEM).decode(), key_pem


def keyos_answer(
    request_xml: bytes,
    *,
    client_cert_pem: str,
    server_key_pem: str,
    server_cert_pem: str,
    keys: dict[str, str],
    with_mac: bool = True,
) -> str:
    """What KeyOS sends back: keys wrapped to the certificate that asked for them."""
    from Crypto.Cipher import AES, PKCS1_OAEP
    from Crypto.PublicKey import RSA
    from Crypto.Util.Padding import pad
    from lxml import etree
    from signxml import XMLSigner, XMLVerifier

    # The server verifies the request was signed by the certificate it carries.
    request = etree.fromstring(request_xml)
    XMLVerifier().verify(request, x509_cert=client_cert_pem)

    cpix, enc, pskc = CPIX_NS["cpix"], CPIX_NS["enc"], CPIX_NS["pskc"]
    document_key = b"\xa5" * 32
    mac_key = b"\x5a" * 64
    to_client = PKCS1_OAEP.new(RSA.import_key(client_cert_pem))

    def wrapped(blob: bytes) -> str:
        return base64.b64encode(to_client.encrypt(blob)).decode()

    root = etree.Element(f"{{{cpix}}}CPIX", nsmap=CPIX_NS)
    delivery_list = etree.SubElement(root, f"{{{cpix}}}DeliveryDataList")
    delivery = etree.SubElement(delivery_list, f"{{{cpix}}}DeliveryData")
    document_element = etree.SubElement(delivery, f"{{{cpix}}}DocumentKey")
    data = etree.SubElement(document_element, f"{{{enc}}}EncryptedKey")
    cipher = etree.SubElement(data, f"{{{enc}}}CipherData")
    etree.SubElement(cipher, f"{{{enc}}}CipherValue").text = wrapped(document_key)

    if with_mac:
        mac_element = etree.SubElement(delivery, f"{{{cpix}}}MACMethod")
        mac_data = etree.SubElement(mac_element, f"{{{enc}}}EncryptedKey")
        mac_cipher = etree.SubElement(mac_data, f"{{{enc}}}CipherData")
        etree.SubElement(mac_cipher, f"{{{enc}}}CipherValue").text = wrapped(mac_key)

    key_list = etree.SubElement(root, f"{{{cpix}}}ContentKeyList")
    for kid, content_key in keys.items():
        element = etree.SubElement(key_list, f"{{{cpix}}}ContentKey", kid=as_uuid(kid))
        secret = etree.SubElement(element, f"{{{cpix}}}Data")
        value = etree.SubElement(secret, f"{{{pskc}}}Secret")
        plain = etree.SubElement(value, f"{{{pskc}}}EncryptedValue")
        cipher_data = etree.SubElement(plain, f"{{{enc}}}CipherData")

        iv = b"\x11" * 16
        blob = iv + AES.new(document_key, AES.MODE_CBC, iv=iv).encrypt(
            pad(bytes.fromhex(content_key), AES.block_size)
        )
        etree.SubElement(cipher_data, f"{{{enc}}}CipherValue").text = base64.b64encode(
            blob
        ).decode()

        if with_mac:
            etree.SubElement(value, f"{{{pskc}}}ValueMAC").text = base64.b64encode(
                hmac.new(mac_key, blob, hashlib.sha512).digest()
            ).decode()

    signed = XMLSigner(c14n_algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315").sign(
        root, key=server_key_pem.encode(), cert=server_cert_pem
    )
    return etree.tostring(signed).decode()


# -- the request -------------------------------------------------------------


def test_the_request_is_signed_and_verifies_against_the_client_certificate(
    credentials: tuple[str, str],
) -> None:
    """An unsigned or wrongly signed document is what KeyOS refuses, so this is the test."""
    from lxml import etree
    from signxml import XMLVerifier

    cert_pem, key_pem = credentials
    xml = build_request(
        [KID], content_id="MovieHubAU", client_cert_pem=cert_pem, client_key_pem=key_pem
    )

    XMLVerifier().verify(etree.fromstring(xml), x509_cert=cert_pem)


def test_the_request_names_the_content_and_every_key_it_wants(
    credentials: tuple[str, str],
) -> None:
    from lxml import etree

    cert_pem, key_pem = credentials
    xml = build_request(
        [KID, OTHER_KID],
        content_id="MovieHubAU",
        client_cert_pem=cert_pem,
        client_key_pem=key_pem,
    )
    root = etree.fromstring(xml)

    assert root.get("contentId") == "MovieHubAU"
    assert root.get("name") == "MovieHubAU"

    asked = {e.get("kid") for e in root.xpath("//cpix:ContentKey", namespaces=CPIX_NS)}
    assert asked == {as_uuid(KID), as_uuid(OTHER_KID)}

    systems = root.xpath("//cpix:DRMSystem", namespaces=CPIX_NS)
    assert {e.get("systemId") for e in systems} == {"edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"}

    carried = root.xpath("//cpix:DeliveryKey//ds:X509Certificate", namespaces=CPIX_NS)
    assert carried, "the client certificate rides along, or the answer cannot be encrypted to us"


def test_a_key_identifier_that_is_not_one_is_left_out_rather_than_sent(
    credentials: tuple[str, str],
) -> None:
    from lxml import etree

    cert_pem, key_pem = credentials
    xml = build_request(
        [KID, "not-a-kid"], content_id="c", client_cert_pem=cert_pem, client_key_pem=key_pem
    )

    asked = etree.fromstring(xml).xpath("//cpix:ContentKey", namespaces=CPIX_NS)
    assert len(asked) == 1


# -- the response ------------------------------------------------------------


def test_the_key_that_went_in_is_the_key_that_comes_out(credentials: tuple[str, str]) -> None:
    """The whole round trip: sign, wrap to the certificate, unwrap in order."""
    cert_pem, key_pem = credentials
    request = build_request([KID], content_id="c", client_cert_pem=cert_pem, client_key_pem=key_pem)
    answer = keyos_answer(
        request,
        client_cert_pem=cert_pem,
        server_key_pem=key_pem,
        server_cert_pem=cert_pem,
        keys={KID: CONTENT_KEY},
    )

    found = parse_response(answer, client_key_pem=key_pem, server_cert_pem=cert_pem)

    assert found == {KID: CONTENT_KEY}


def test_several_keys_come_back_keyed_by_their_identifiers(
    credentials: tuple[str, str],
) -> None:
    cert_pem, key_pem = credentials
    other = "ffeeddccbbaa99887766554433221100"
    request = build_request(
        [KID, OTHER_KID], content_id="c", client_cert_pem=cert_pem, client_key_pem=key_pem
    )
    answer = keyos_answer(
        request,
        client_cert_pem=cert_pem,
        server_key_pem=key_pem,
        server_cert_pem=cert_pem,
        keys={KID: CONTENT_KEY, OTHER_KID: other},
    )

    assert parse_response(answer, client_key_pem=key_pem, server_cert_pem=cert_pem) == {
        KID: CONTENT_KEY,
        OTHER_KID: other,
    }


def test_a_response_with_no_mac_is_still_read(credentials: tuple[str, str]) -> None:
    """The MAC is optional in CPIX, so requiring it would refuse a valid answer."""
    cert_pem, key_pem = credentials
    request = build_request([KID], content_id="c", client_cert_pem=cert_pem, client_key_pem=key_pem)
    answer = keyos_answer(
        request,
        client_cert_pem=cert_pem,
        server_key_pem=key_pem,
        server_cert_pem=cert_pem,
        keys={KID: CONTENT_KEY},
        with_mac=False,
    )

    assert parse_response(answer, client_key_pem=key_pem, server_cert_pem=cert_pem) == {
        KID: CONTENT_KEY
    }


def test_a_key_whose_mac_does_not_match_is_dropped(credentials: tuple[str, str]) -> None:
    """A key that fails its own integrity check is not a key, and decrypting with it would
    turn a readable segment into noise the bitstream rules would report as corruption."""
    cert_pem, key_pem = credentials
    request = build_request([KID], content_id="c", client_cert_pem=cert_pem, client_key_pem=key_pem)
    answer = keyos_answer(
        request,
        client_cert_pem=cert_pem,
        server_key_pem=key_pem,
        server_cert_pem=cert_pem,
        keys={KID: CONTENT_KEY},
    ).replace("<pskc:ValueMAC>", "<pskc:ValueMAC>AAAA", 1)

    assert parse_response(answer, client_key_pem=key_pem, server_cert_pem=cert_pem) == {}


def test_an_answer_with_no_document_key_is_refused_with_a_sentence(
    credentials: tuple[str, str],
) -> None:
    _, key_pem = credentials

    with pytest.raises(CpixError) as raised:
        parse_response(
            '<cpix:CPIX xmlns:cpix="urn:dashif:org:cpix"/>',
            client_key_pem=key_pem,
            server_cert_pem="",
        )

    assert "no document key" in str(raised.value)


def test_an_answer_that_is_not_xml_is_refused_with_a_sentence(
    credentials: tuple[str, str],
) -> None:
    """What a proxy in front of the key server returns when it will not pass the call on."""
    _, key_pem = credentials

    with pytest.raises(CpixError) as raised:
        parse_response("502 Bad Gateway", client_key_pem=key_pem, server_cert_pem="")

    assert "does not parse as XML" in str(raised.value)


def test_an_answer_that_is_xml_but_not_a_cpix_document_is_refused_too(
    credentials: tuple[str, str],
) -> None:
    """An error page can be well-formed XML and still carry no key."""
    _, key_pem = credentials

    with pytest.raises(CpixError) as raised:
        parse_response("<html>502 Bad Gateway</html>", client_key_pem=key_pem, server_cert_pem="")

    assert "carries no document key" in str(raised.value)


# -- the store ---------------------------------------------------------------


@pytest.mark.asyncio()
async def test_a_key_is_asked_for_once_and_kept_for_the_run() -> None:
    """
    A live channel asks for the same key on every segment of every rendition.

    Without the store that is thousands of signed requests an hour to the key server for an
    answer that never changes.
    """
    store = KeyStore(credentials=CpixCredentials(), content_id="c")
    store._keys[KID] = CONTENT_KEY

    assert await store.key_for(KID) == CONTENT_KEY
    assert await store.key_for(KID.upper()) == CONTENT_KEY, "however the caller spells it"
    assert store.requests_made == 0, "the held key was used"


@pytest.mark.asyncio()
async def test_a_store_with_no_credentials_says_so_rather_than_failing_obscurely() -> None:
    store = KeyStore(credentials=CpixCredentials(), content_id="c")

    with pytest.raises(CpixError) as raised:
        await store.key_for(KID)

    assert "No CPIX credentials are configured" in str(raised.value)
    assert "Settings" in str(raised.value), "the message says where to fix it"


@pytest.mark.asyncio()
async def test_an_identifier_that_is_not_one_asks_for_nothing() -> None:
    store = KeyStore(credentials=CpixCredentials(), content_id="c")

    assert await store.key_for("") == ""
    assert await store.key_for("not-a-kid") == ""
    assert store.requests_made == 0


def test_what_the_ui_may_know_about_the_credentials_is_not_the_credentials() -> None:
    """A path can carry a hostname, and the private key's whereabouts is not for a page."""
    described = CpixCredentials(
        client_cert="/etc/rba/client.pem",
        client_key="/etc/rba/secret.key",
        server_cert="/etc/rba/keyos.pem",
    ).describe()

    assert described["configured"] is True
    assert described["client_key_set"] is True
    assert "/etc/rba/secret.key" not in str(described)
    assert "secret" not in str(described)
