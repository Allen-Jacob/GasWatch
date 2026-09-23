import respx

from app.notifications.ntfy import NtfyNotifier


@respx.mock
async def test_ntfy_uses_topic_path_and_bearer_token() -> None:
    route = respx.post("https://ntfy.example.test/gaswatch").respond(200)
    notifier = NtfyNotifier("https://ntfy.example.test", "gaswatch", "secret")
    try:
        await notifier.send("Titre", "Message", "high")
    finally:
        await notifier.close()
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer secret"
    assert request.headers["Title"] == "=?utf-8?q?Titre?="
    assert request.headers["Priority"] == "high"
    assert request.content == b"Message"


@respx.mock
async def test_ntfy_escapes_topic_and_encodes_unicode_title() -> None:
    route = respx.post("https://ntfy.example.test/gaswatch%20priv%C3%A9").respond(200)
    notifier = NtfyNotifier("https://ntfy.example.test", "gaswatch privé")
    try:
        await notifier.send("⛽ GasWatch — Test", "Ça fonctionne")
    finally:
        await notifier.close()

    request = route.calls[0].request
    assert request.headers["Title"].isascii()
    assert request.content == "Ça fonctionne".encode()
