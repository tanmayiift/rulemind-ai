"""SAML assertion validation (Tier 3.5).

A verified XML-DSig signature proves the IdP said something. It does not prove the IdP said it
recently, said it to US, said it for THIS service, or said it only once. None of that was checked,
and the assertion the code read was not even required to be the signed one:

  * **Signature wrapping (XSW).** The code fell back to `root.find(".//saml:Assertion")` -- the
    UNVERIFIED document -- when the signed subtree contained no Assertion. Worth being precise
    about the severity: with either of the two standard IdP signing configurations (sign the
    Response, or sign the Assertion) that fallback is not reached, so this was a LATENT hole
    rather than a live exploit -- it needed a signed subtree that was neither an Assertion nor
    contained one. The fix removes it rather than relying on which shape an IdP happens to send,
    and the test below pins the branch that is actually load-bearing: when the signed subtree IS
    an Assertion, the identity must come from THAT element and not from a document-order search
    that a forged sibling can win.
  * **No Conditions check.** No NotBefore/NotOnOrAfter, so an assertion was valid for as long as
    the IdP's certificate -- one captured a year ago still logs you in.
  * **No AudienceRestriction check.** An assertion the IdP minted for a DIFFERENT service provider
    was accepted here, so any other SP sharing the customer's IdP could impersonate its users.
  * **No Recipient/Destination check.** Same problem at the endpoint level.
  * **No replay tracking.** SAML has no built-in replay defence; an assertion is a signed bearer
    token, so one captured from a proxy log replayed for its whole validity window.
  * **Entity expansion enabled pre-auth.** `etree.fromstring` with the default parser resolves
    entities on an attacker-supplied document -- billion laughs, or file disclosure.
"""
from __future__ import annotations

import base64
import datetime as dt
import os
import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from lxml import etree  # noqa: E402
from signxml import XMLSigner  # noqa: E402

from app import sso  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests.test_sso import (  # noqa: E402
    ACS_URL, _rsa_keypair, _saml_config, _saml_response_xml,
    _self_signed_cert, _signed_saml_response,
)


class SignatureWrappingTest(unittest.TestCase):
    """The headline. A signature over Jane must not authenticate Mallory."""

    def setUp(self):
        self.key, self.pem = _rsa_keypair()
        self.cert = _self_signed_cert(self.key)

    def _wrapped_document(self):
        """A wrapping payload whose signature genuinely verifies.

        The IdP signs the ASSERTION (one of the two standard configurations). The attacker keeps
        that signed assertion byte-for-byte -- so verification passes on it -- and wraps it in
        their own Response alongside a FORGED unsigned assertion placed FIRST in document order.

        Any code that locates the assertion by searching the document, rather than by using the
        element the signature actually covered, picks up Mallory's.
        """
        ns = {"saml": "urn:oasis:names:tc:SAML:2.0:assertion"}
        genuine = etree.fromstring(
            _saml_response_xml("jane@acme.com").encode()).find(".//saml:Assertion", ns)
        signed_assertion = XMLSigner().sign(genuine, key=self.pem.encode(),
                                            cert=self.cert.encode())
        forged = etree.fromstring(
            _saml_response_xml("mallory@evil.com", name="Mallory",
                               assertion_id="_forged").encode()).find(".//saml:Assertion", ns)
        wrapper = etree.fromstring(
            '<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
            'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_r" Version="2.0" '
            'IssueInstant="2026-01-01T00:00:00Z" Destination="{0}"/>'.format(ACS_URL))
        wrapper.append(forged)             # first in document order
        wrapper.append(signed_assertion)   # genuine, signed, untouched
        return base64.b64encode(etree.tostring(wrapper)).decode()

    def test_the_identity_comes_from_the_signed_element_not_a_document_search(self):
        """The load-bearing assertion. This payload's signature VERIFIES, so a rejection is not
        what protects us here -- using the signed element is."""
        payload = self._wrapped_document()
        try:
            identity = sso.parse_and_verify_saml_response(_saml_config(self.cert), payload)
        except sso.SsoError:
            return  # rejected outright is also a safe outcome
        self.assertNotEqual(identity["email"], "mallory@evil.com",
                            "a forged unsigned assertion was accepted as the identity")
        self.assertEqual(identity["email"], "jane@acme.com",
                         "the identity must come from the SIGNED assertion")

    def test_the_unverified_document_fallback_is_gone(self):
        """Structural: the fallback was one line, and its absence is the fix. Asserting on the
        source keeps someone from reinstating it as a 'compatibility' convenience."""
        import inspect

        source = inspect.getsource(sso.parse_and_verify_saml_response)
        self.assertNotIn('assertion = root.find(".//saml:Assertion"', source,
                         "the XSW fallback to the unverified document is back")

    def test_a_genuine_response_still_works(self):
        """The regression guard. Every rejection above is only correct if real logins survive."""
        resp = _signed_saml_response(self.cert, self.pem, "jane@acme.com")
        identity = sso.parse_and_verify_saml_response(_saml_config(self.cert), resp)
        self.assertEqual(identity["email"], "jane@acme.com")


class ConditionsTest(unittest.TestCase):
    def setUp(self):
        self.key, self.pem = _rsa_keypair()
        self.cert = _self_signed_cert(self.key)

    def _verify(self, **kwargs):
        resp = _signed_saml_response(self.cert, self.pem, "jane@acme.com", **kwargs)
        return sso.parse_and_verify_saml_response(_saml_config(self.cert), resp)

    def test_an_expired_assertion_is_rejected(self):
        with self.assertRaises(sso.SsoError) as caught:
            self._verify(minutes_ago=120, minutes_ahead=-60)
        self.assertIn("expired", str(caught.exception).lower())

    def test_a_not_yet_valid_assertion_is_rejected(self):
        with self.assertRaises(sso.SsoError) as caught:
            self._verify(minutes_ago=-60, minutes_ahead=120)
        self.assertIn("not yet valid", str(caught.exception).lower())

    def test_clock_skew_is_tolerated(self):
        """A one-minute-in-the-future NotBefore is a clock difference, not an attack. Rejecting it
        would break real logins against an IdP whose clock runs slightly ahead."""
        identity = self._verify(minutes_ago=-1, minutes_ahead=30)
        self.assertEqual(identity["email"], "jane@acme.com")

    def test_a_missing_conditions_element_is_rejected(self):
        with self.assertRaises(sso.SsoError) as caught:
            self._verify(conditions=False)
        self.assertIn("conditions", str(caught.exception).lower())

    def test_an_assertion_for_another_service_provider_is_rejected(self):
        """The cross-SP attack: any other SP sharing the customer's IdP could otherwise replay an
        assertion minted for itself and become that user here."""
        with self.assertRaises(sso.SsoError) as caught:
            self._verify(audience="https://some-other-sp.example/sp")
        self.assertIn("audience", str(caught.exception).lower())

    def test_a_missing_audience_restriction_is_rejected(self):
        with self.assertRaises(sso.SsoError):
            self._verify(audience="")

    def test_a_wrong_recipient_is_rejected(self):
        with self.assertRaises(sso.SsoError) as caught:
            self._verify(recipient="https://attacker.example/acs")
        self.assertIn("recipient", str(caught.exception).lower())

    def test_a_wrong_destination_is_rejected(self):
        with self.assertRaises(sso.SsoError) as caught:
            self._verify(destination="https://attacker.example/acs")
        self.assertIn("destination", str(caught.exception).lower())

    def test_an_absent_destination_is_tolerated(self):
        """Not every IdP sets Destination. Absent is fine; present and wrong is not."""
        self.assertEqual(self._verify(destination="")["email"], "jane@acme.com")


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.storage = Storage(path=os.path.join(self.tempdir.name, "saml.db"))
        self.tenant = self.storage.default_tenant_id
        self.key, self.pem = _rsa_keypair()
        self.cert = _self_signed_cert(self.key)

    def tearDown(self):
        self.tempdir.cleanup()

    def _verify(self, resp):
        return sso.parse_and_verify_saml_response(
            _saml_config(self.cert), resp, storage=self.storage, tenant_id=self.tenant)

    def test_an_assertion_works_once(self):
        resp = _signed_saml_response(self.cert, self.pem, "jane@acme.com")
        self.assertEqual(self._verify(resp)["email"], "jane@acme.com")

    def test_the_same_assertion_cannot_be_replayed(self):
        resp = _signed_saml_response(self.cert, self.pem, "jane@acme.com")
        self._verify(resp)
        with self.assertRaises(sso.SsoError) as caught:
            self._verify(resp)
        self.assertIn("already been used", str(caught.exception))

    def test_a_distinct_assertion_still_works(self):
        """Single-use must not mean single-login -- a second, genuinely new assertion is fine."""
        self._verify(_signed_saml_response(self.cert, self.pem, "jane@acme.com",
                                           assertion_id="_first"))
        second = self._verify(_signed_saml_response(self.cert, self.pem, "jane@acme.com",
                                                    assertion_id="_second"))
        self.assertEqual(second["email"], "jane@acme.com")

    def test_replay_is_scoped_per_tenant(self):
        """Two IdPs can legitimately mint the same assertion ID; one tenant consuming an id must
        not block another, nor let one probe the other's id space."""
        other = self.storage.create_tenant("Other Co")["id"]
        resp = _signed_saml_response(self.cert, self.pem, "jane@acme.com", assertion_id="_shared")
        self._verify(resp)
        accepted = sso.parse_and_verify_saml_response(
            _saml_config(self.cert), resp, storage=self.storage, tenant_id=other)
        self.assertEqual(accepted["email"], "jane@acme.com")

    def test_consumed_assertions_are_prunable(self):
        """Otherwise the table grows forever."""
        self.storage.claim_saml_assertion(
            "_old", not_on_or_after=dt.datetime.utcnow() - dt.timedelta(hours=1),
            tenant_id=self.tenant)
        self.storage.claim_saml_assertion(
            "_current", not_on_or_after=dt.datetime.utcnow() + dt.timedelta(hours=1),
            tenant_id=self.tenant)
        self.assertEqual(self.storage.prune_saml_assertions(tenant_id=self.tenant), 1)
        # The still-valid one must survive, or pruning would re-open the replay window.
        self.assertFalse(self.storage.claim_saml_assertion("_current", tenant_id=self.tenant))

    def test_an_assertion_with_no_id_is_rejected_when_replay_tracking_is_on(self):
        """Without an ID single-use cannot be enforced, so accepting it would be a silent hole."""
        xml = _saml_response_xml("jane@acme.com").replace('ID="_assert1" ', "", 1)
        root = etree.fromstring(xml.encode())
        signed = XMLSigner().sign(root, key=self.pem.encode(), cert=self.cert.encode())
        resp = base64.b64encode(etree.tostring(signed)).decode()
        with self.assertRaises(sso.SsoError):
            self._verify(resp)


class XmlParserTest(unittest.TestCase):
    def setUp(self):
        self.key, self.pem = _rsa_keypair()
        self.cert = _self_signed_cert(self.key)

    def test_external_entities_are_not_resolved(self):
        """Runs PRE-AUTH on an attacker-supplied document, so an external entity here is file
        disclosure from the API process."""
        payload = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE root [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            '<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol">'
            '<leak>&xxe;</leak></samlp:Response>'
        )
        encoded = base64.b64encode(payload.encode()).decode()
        with self.assertRaises(sso.SsoError) as caught:
            sso.parse_and_verify_saml_response(_saml_config(self.cert), encoded)
        self.assertNotIn("root:", str(caught.exception),
                         "an external entity was resolved and its content surfaced")

    def test_the_parser_does_not_expand_entities(self):
        """Behavioural, not attribute-based: lxml does not expose resolve_entities on the parser,
        so assert what actually matters -- a defined entity's text does not appear in the tree."""
        payload = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE root [<!ENTITY boom "EXPANDED-SECRET">]>'
            '<root><leak>&boom;</leak></root>'
        )
        tree = etree.fromstring(payload.encode(), parser=sso._saml_parser())
        self.assertNotIn("EXPANDED-SECRET", etree.tostring(tree).decode(),
                         "the SAML parser expanded an internal entity")

    def test_a_billion_laughs_document_does_not_expand(self):
        """The DoS form. Nested entities that each reference the previous one expand
        exponentially; with resolution off there is nothing to expand."""
        payload = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE lolz ['
            '<!ENTITY lol "lol">'
            '<!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
            '<!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">'
            ']>'
            '<lolz>&lol2;</lolz>'
        )
        tree = etree.fromstring(payload.encode(), parser=sso._saml_parser())
        self.assertLess(len(etree.tostring(tree)), 500,
                        "entity expansion produced a large document")


if __name__ == "__main__":
    unittest.main()
