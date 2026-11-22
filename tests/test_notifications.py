import pytest

from idpatent.domain.enums import LawfulBasis, Purpose
from idpatent.notifications.esp import OutboundEmail, OutboxProvider, validate_outbound
from idpatent.notifications.renderer import (
    TEMPLATE_KEYS, FooterLinks, SenderIdentity, TemplateRenderer, TemplateValidationError,
)

SENDER = SenderIdentity("PT Contoh Konsultan HKI", "Jl. Jend. Sudirman Kav. 1, Jakarta 10220", "hki@contoh.co.id", "+62 21 555 0100")
LINKS = FooterLinks("https://app.example.id/o/abc", "https://app.example.id/p/abc", "https://app.example.id/privasi")
PATENT = dict(
    number="IDP000012345", type_label="Paten", title="Alat penyaring air", annuity_years="Y13",
    annuity_due="10 Februari 2027", grace_end="10 Agustus 2027", surcharge_percent=100, ws_due="31 Desember 2026",
    ws_period="2026", protection_end="10 Maret 2035", filing_date="10 Maret 2015", status_label="Diberi",
)
CTX = dict(recipient_name="Ibu Sari", owner_name="PT Air Bersih", patents=[PATENT], services=["Pembayaran biaya tahunan"],
           data_source_text="PDKI", as_of_text="22 November 2026")


@pytest.mark.parametrize("purpose", list(Purpose))
@pytest.mark.parametrize("lang", ["id", "en"])
def test_every_template_renders_with_footer(purpose, lang):
    r = TemplateRenderer().render(TEMPLATE_KEYS[purpose], lang, CTX, sender=SENDER, links=LINKS, purpose=purpose,
                                  lawful_basis=LawfulBasis.CONTRACT, is_cold=False)
    assert "IDP000012345" in r.subject or "PT Air Bersih" in r.subject
    assert SENDER.address in r.text and LINKS.unsubscribe_url in r.text and LINKS.privacy_url in r.text
    assert len(r.template_hash) == 64
    assert '<a href="https://app.example.id/o/abc">' in r.html


def test_cold_footer_explains_source():
    r = TemplateRenderer().render("annuity_reminder", "id", CTX, sender=SENDER, links=LINKS,
                                  purpose=Purpose.ANNUITY_REMINDER, lawful_basis=LawfulBasis.LEGITIMATE_INTEREST, is_cold=True)
    assert "basis data publik PDKI" in r.text and "Kepentingan yang sah" in r.text


def test_non_https_rejected():
    bad = FooterLinks("http://insecure/o", LINKS.preferences_url, LINKS.privacy_url)
    with pytest.raises(TemplateValidationError) as exc:
        TemplateRenderer().render("annuity_reminder", "en", CTX, sender=SENDER, links=bad,
                                  purpose=Purpose.ANNUITY_REMINDER, lawful_basis=LawfulBasis.CONTRACT, is_cold=False)
    assert "NON_HTTPS_LINK" in exc.value.errors


def test_missing_variable_fails_loudly():
    with pytest.raises(Exception):
        TemplateRenderer().render("annuity_reminder", "id", {"patents": [PATENT]}, sender=SENDER, links=LINKS,
                                  purpose=Purpose.ANNUITY_REMINDER, lawful_basis=None, is_cold=False)


def test_outbox_headers_and_attachment_block():
    msg = OutboundEmail("a@b.co", "hki@contoh.co.id", "Contoh", "Subj", "t", "<p>t</p>", LINKS.unsubscribe_url,
                        unsubscribe_mailto="unsub@contoh.co.id")
    h = msg.headers()
    assert h["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "<mailto:unsub@contoh.co.id>" in h["List-Unsubscribe"]
    outbox = OutboxProvider()
    assert outbox.send(msg).startswith("outbox-")
    msg.attachments.append(("invoice.exe", b"x", "application/octet-stream"))
    assert "EXECUTABLE_ATTACHMENT" in validate_outbound(msg)
    with pytest.raises(ValueError):
        outbox.send(msg)


def test_templates_available():
    avail = TemplateRenderer().available()
    for key in TEMPLATE_KEYS.values():
        assert (key, "id") in avail and (key, "en") in avail
