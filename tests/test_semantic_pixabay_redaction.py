from pathlib import Path
from types import SimpleNamespace
import pytest
import requests
from kadr_farm import pixabay_media

class FailedResponse:
    status_code=401
    def raise_for_status(self): raise requests.HTTPError("request URL had token SECRET-PIXABAY-KEY")

def test_pixabay_errors_redact_configured_api_key(tmp_path, monkeypatch):
    monkeypatch.setattr(pixabay_media.requests, "get", lambda *a, **k: FailedResponse())
    settings=SimpleNamespace(pixabay_api_key="SECRET-PIXABAY-KEY")
    with pytest.raises(pixabay_media.PixabayError) as exc:
        pixabay_media.fetch_pixabay(settings, "robot", "photo", tmp_path)
    assert "SECRET-PIXABAY-KEY" not in str(exc.value)
    assert "[REDACTED]" in str(exc.value)
