import respx

from app.notifications.ntfy import NtfyNotifier


@respx.mock
async def test_ntfy_uses_json_and_bearer_token() -> None:
    route = respx.post("https://ntfy.example.test/").respond(200)
    notifier = NtfyNotifier("https://ntfy.example.test", "gaswatch", "secret")
    try:
        await notifier.send("Titre", "Message", "high")
    finally:
        await notifier.close()
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer secret"
    assert b'"topic":"gaswatch"' in request.content
