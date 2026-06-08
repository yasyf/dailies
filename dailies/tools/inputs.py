from __future__ import annotations

from dataclasses import dataclass

from dailies.models import FrozenModel
from dailies.runtime import RunContext
from dailies.tools.base import ToolSet, tool


class EmailMessage(FrozenModel):
    id: str
    thread_id: str
    sender: str
    subject: str
    body: str


@dataclass(frozen=True, slots=True)
class EmailToolSet(ToolSet):
    context: RunContext

    @tool
    async def get_thread(self, thread_id: str) -> list[EmailMessage]:
        """Return all messages in an email thread."""
        raise NotImplementedError

    @tool
    async def get_message(self, message_id: str) -> EmailMessage:
        """Return a single email message by id."""
        raise NotImplementedError

    @tool
    async def search_emails(self, query: str) -> list[EmailMessage]:
        """Search emails and return matching messages."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class BrowserToolSet(ToolSet):
    context: RunContext

    @tool
    async def fetch_url(self, url: str) -> str:
        """Fetch a URL and return its text content."""
        raise NotImplementedError

    @tool
    async def search_web(self, query: str) -> str:
        """Search the web and return a summary of results."""
        raise NotImplementedError
