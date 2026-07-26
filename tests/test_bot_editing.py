import asyncio
from unittest.mock import AsyncMock

from bot import safe_edit_text


class DummyMessage:
    def __init__(self, caption=None):
        self.caption = caption
        self.photo = [object()] if caption is not None else []


class DummyQuery:
    def __init__(self, caption=None):
        self.message = DummyMessage(caption=caption)
        self.edit_message_text = AsyncMock()
        self.edit_message_caption = AsyncMock()


def test_safe_edit_text_uses_caption_for_photo_messages():
    query = DummyQuery(caption="existing caption")

    asyncio.run(safe_edit_text(query, "new caption"))

    query.edit_message_caption.assert_awaited_once()
    query.edit_message_text.assert_not_awaited()
