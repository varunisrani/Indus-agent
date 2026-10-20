"""Reading untrusted mail: decoding, addresses, bodies, sender authentication (P16-09)."""

from __future__ import annotations

from email import message_from_bytes
from email.message import EmailMessage

import pytest

from indus.gateway.adapters.email.parsing import (
    MAX_FROM_CHARS,
    attachments_of,
    body_text,
    clean_ids,
    decode_header_value,
    display_name_of,
    html_to_text,
    is_automated,
    one_line,
    safe_decode,
    sender_address,
    verify_sender,
)

PIN = "mx.example.net"


def test_decoding_never_raises() -> None:
    assert safe_decode(b"caf\xc3\xa9", "unknown-8bit") == "café"
    assert safe_decode(b"abc", "x-no-such-charset") == "abc"
    assert safe_decode(b"abc", None) == "abc"
    assert safe_decode(b"\x81\x30\x81\x30", "gb2312") != ""  # GBK extension via gb18030
    assert "\ufffd" in safe_decode(b"\xff\xfe broken", "utf-8")
    assert decode_header_value("=?utf-8?b?SGVsbG8gd29ybGQ=?=") == "Hello world"
    assert decode_header_value("=?utf-8?q?caf=C3=A9?= au lait") == "café au lait"
    assert decode_header_value("=?x-bogus?q?abc?=") == "abc"
    assert decode_header_value("plain words") == "plain words"
    assert decode_header_value(None) == ""


def test_one_line_removes_header_injection() -> None:
    assert one_line("Hi\r\nBcc: victim@example.org") == "Hi Bcc: victim@example.org"
    assert len(one_line("x" * 2000)) == 998


@pytest.mark.parametrize(
    ("raw", "address"),
    [
        ("Ana <Ana@Example.ORG>", "ana@example.org"),
        ("ana@example.org", "ana@example.org"),
        ('"evil <boss@corp.example>" <real@example.org>', "real@example.org"),
        ('"Folded\r\n Name" <ana@example.org>', "ana@example.org"),
        ("Doe, John <john@example.org>", "john@example.org"),
        ("john@example.org <john@example.org>", "john@example.org"),
        ("a@example.org, b@example.org", ""),
        ("John", ""),
        ("John <john>", ""),
        ("", ""),
        ("x" * MAX_FROM_CHARS + " <a@example.org>", ""),
        ("(" * 65 + "a@example.org", ""),
        ("Mallory <boss@corp.example> <mallory@evil.example>", ""),
    ],
)
def test_sender_address(raw: str, address: str) -> None:
    assert sender_address(raw) == address


def test_display_name() -> None:
    assert display_name_of('"Ana Lima" <ana@example.org>') == "Ana Lima"
    assert display_name_of("=?utf-8?q?Jos=C3=A9?= <jose@example.org>") == "José"


@pytest.mark.parametrize(
    ("address", "headers", "automated"),
    [
        ("ana@example.org", {}, False),
        ("ana@example.org", {"Auto-Submitted": "auto-replied"}, True),
        ("ana@example.org", {"Auto-Submitted": "no"}, False),
        ("ana@example.org", {"Precedence": "bulk"}, True),
        ("ana@example.org", {"Precedence": "list"}, True),
        ("ana@example.org", {"List-Id": "<team.lists.example.org>"}, True),
        ("ana@example.org", {"List-Unsubscribe": "<mailto:u@example.org>"}, True),
        ("noreply@example.org", {}, True),
        ("MAILER-DAEMON@example.org", {}, True),
        ("ana@bouncecastle.example", {}, False),
    ],
)
def test_automated_mail(address: str, headers: dict[str, str], automated: bool) -> None:
    assert is_automated(address, headers) is automated


def test_html_becomes_text_without_scripts_styles_or_remote_content() -> None:
    html = (
        "<html><head><title>T</title><style>p{color:red}</style></head><body>"
        "<p>Hello &amp; welcome</p><script>alert(1)</script>"
        "<img src='https://tracker.example/pixel.gif'>"
        "<ul><li>one</li><li>two</li></ul>line<br>break</body></html>"
    )
    text = html_to_text(html)
    assert "alert" not in text and "color" not in text and "tracker" not in text
    assert "Hello & welcome" in text
    assert "- one" in text and "- two" in text
    assert "line\nbreak" in text


def _mail(*, plain: str | None, html: str | None, attach: bool = False) -> EmailMessage:
    message = EmailMessage()
    message["From"] = "ana@example.org"
    if plain is not None:
        message.set_content(plain)
    if html is not None:
        if plain is None:
            message.set_content(html, subtype="html")
        else:
            message.add_alternative(html, subtype="html")
    if attach:
        message.add_attachment(b"a,b\n1,2\n", maintype="text", subtype="csv", filename="d.csv")
        message.add_attachment(
            b"\x89PNG\r\n\x1a\n", maintype="image", subtype="png", filename="p.png"
        )
    return message


def test_bodies() -> None:
    assert body_text(_mail(plain="plain wins", html="<p>html</p>")) == "plain wins"
    assert body_text(_mail(plain=None, html="<p>only <b>html</b></p>")) == "only html"
    with_files = _mail(plain="see attached", html=None, attach=True)
    assert body_text(with_files) == "see attached"
    parts = attachments_of(with_files)
    assert [(p.name, p.mime) for p in parts] == [("d.csv", "text/csv"), ("p.png", "image/png")]
    assert parts[0].data == b"a,b\n1,2\n"
    assert len(attachments_of(with_files, limit=1)) == 1


def test_broken_charset_body_does_not_raise() -> None:
    raw = b"From: ana@example.org\r\nContent-Type: text/plain; charset=bogus-8\r\n\r\ncaf\xe9\r\n"
    assert body_text(message_from_bytes(raw)).startswith("caf")


def test_clean_ids() -> None:
    assert clean_ids("<a@x>", "<b@x>\r\nBcc: v@x <a@x>") == ["<a@x>", "<b@x>"]
    assert clean_ids("not an id") == []
    many = " ".join(f"<{n}@x>" for n in range(30))
    assert clean_ids(many)[0] == "<10@x>" and len(clean_ids(many)) == 20


@pytest.mark.parametrize(
    ("headers", "verdict"),
    [
        ([f"{PIN}; dmarc=pass header.from=example.org"], True),
        ([f"{PIN} 1; dmarc=pass header.from=example.org"], True),
        ([f"{PIN}; dmarc=pass"], True),
        ([f"{PIN}; dmarc=pass header.from=evil.example"], False),
        ([f"{PIN}; dmarc=fail header.from=example.org"], False),
        ([f"{PIN}; dmarc=pass header.from=example.org; dmarc=pass"], False),
        (["other.example; dmarc=pass header.from=example.org"], False),
        (["evil.example; x=y", f"{PIN}; dmarc=pass header.from=example.org"], False),
        ([], False),
        ([f"{PIN}; spf=pass smtp.mailfrom=bounces@mail.example.org"], True),
        ([f"{PIN}; spf=pass smtp.mailfrom=x@evil.example"], False),
        (
            [f"{PIN}; spf=pass smtp.mailfrom=a@example.org; spf=fail smtp.mailfrom=a@example.org"],
            False,
        ),
        ([f"{PIN}; dkim=pass header.d=example.org header.s=s1"], True),
        ([f"{PIN}; dkim=fail header.d=example.org; dkim=pass header.d=sub.example.org"], True),
        ([f"{PIN}; dkim=pass header.d=evil.example"], False),
        ([f"{PIN}; dkim=pass header.d=org"], False),
        # Text inside quotes or comments must never count.
        ([f'{PIN}; spf=fail smtp.mailfrom="x spf=pass"@example.org'], False),
        ([f"{PIN}; dkim=fail (dkim=pass header.d=example.org) header.d=evil.example"], False),
        ([f'{PIN}; dkim=fail reason="header.d=example.org dkim=pass"'], False),
        ([f'{PIN}; dmarc=pass "unclosed'], False),
        ([f"{PIN}; dmarc=pass (unclosed"], False),
    ],
)
def test_sender_authentication(headers: list[str], verdict: bool) -> None:
    ok, _reason = verify_sender(headers, "ana@example.org", authserv_id=PIN)
    assert ok is verdict


def test_sender_authentication_needs_a_pin_and_a_domain() -> None:
    header = [f"{PIN}; dmarc=pass header.from=example.org"]
    assert verify_sender(header, "ana@example.org", authserv_id="")[0] is False
    assert verify_sender(header, "nobody", authserv_id=PIN)[0] is False
