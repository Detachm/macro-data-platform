from __future__ import annotations

from fastapi.testclient import TestClient
from pydantic import SecretStr

from macro_platform.api.app import create_app
from macro_platform.config import Settings
from macro_platform.contracts.news import ContentMode, NewsEvent, NewsQuery
from macro_platform.storage.repositories import EmptyDataRepository
from tests.helpers import NOW, news_event

TOKEN = "internal-personal-use-token"


class _NewsRepository(EmptyDataRepository):
    def __init__(self, event: NewsEvent) -> None:
        self._event = event

    async def list_news(self, query: NewsQuery) -> list[NewsEvent]:
        return [self._event]


def test_production_editor_context_keeps_content_regardless_of_legacy_rights() -> None:
    payload = news_event(external_llm_allowed=False).model_dump()
    payload["body"] = "full internal article"
    payload["content_mode"] = ContentMode.FULL_TEXT
    payload["usage_rights"]["storage_allowed"] = False
    event = NewsEvent.model_validate(payload)
    app = create_app(
        settings=Settings(
            app_env="production",
            provider_mode="live",
            provider_cursor_secret=SecretStr("test-provider-cursor-secret"),
            service_token=SecretStr(TOKEN),
            us_provider_mode="live",
            twelve_data_api_key=SecretStr("test-twelve-data-key"),
            twelve_data_cursor_secret=SecretStr("test-twelve-data-cursor"),
        ),
        repository=_NewsRepository(event),
    )

    with TestClient(app) as api:
        response = api.post(
            "/v1/editor/context",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"regions": ["CN"], "as_of": NOW.isoformat()},
        )

    assert response.status_code == 200
    returned = response.json()["data"]["news_events"]
    assert returned[0]["body"] == "full internal article"
    assert returned[0]["summary"] is not None
