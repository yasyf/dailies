from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar, Literal

from dailies.documents import Subscription
from dailies.gmail import EmailMessage, truncate
from dailies.models import FrozenModel, WorkflowId, utcnow
from dailies.runtime import RunContext
from dailies.tools.base import ToolSet, tool

if TYPE_CHECKING:
    from dailies.gmail import GmailClient, MessageMeta


class SubscriptionNotFound(LookupError):
    """No subscription matches the given watch for this workflow."""


class SubscriptionInfo(FrozenModel):
    event: str
    key: str
    watermark: datetime


class SubscriptionUpdate(FrozenModel):
    event: str
    key: str
    messages: list[EmailMessage]


def info(subscription: Subscription) -> SubscriptionInfo:
    return SubscriptionInfo(event=subscription.event, key=subscription.key, watermark=subscription.watermark)


async def insert_subscription(
    workflow_id: WorkflowId, event: str, key: str, *, origin: Literal["trigger", "agent"]
) -> Subscription:
    subscription = Subscription(
        workflow_id=workflow_id, source="gmail", event=event, key=key, watermark=utcnow(), origin=origin
    )
    await subscription.insert()
    return subscription


async def news_since(gmail: GmailClient, subscription: Subscription) -> list[MessageMeta]:
    match subscription.event:
        case "thread":
            metas = await gmail.thread_metas(subscription.key)
        case "query":
            metas = await gmail.query_metas(subscription.key, after=subscription.watermark)
        case event:
            raise ValueError(f"unknown gmail event: {event}")
    return sorted((meta for meta in metas if meta.date > subscription.watermark), key=lambda meta: meta.date)


@dataclass(frozen=True, slots=True)
class EmailToolSet(ToolSet):
    integrations: ClassVar[tuple[str, ...]] = ("gmail",)

    context: RunContext
    gmail: GmailClient

    @tool
    async def get_thread(self, thread_id: str) -> list[EmailMessage]:
        """Return all messages in an email thread, bodies truncated; fetch a full body with get_message."""
        return [truncate(message) for message in await self.gmail.thread(thread_id)]

    @tool
    async def get_message(self, message_id: str) -> EmailMessage:
        """Return a single email message by id with its full body."""
        return await self.gmail.message(message_id)

    @tool
    async def search_emails(self, query: str) -> list[EmailMessage]:
        """Search the mailbox with Gmail query syntax; returns at most 20 matches, bodies truncated."""
        return [truncate(message) for message in await self.gmail.search(query)]

    @tool
    async def subscribe_to_thread(self, thread_id: str) -> SubscriptionInfo:
        """Watch an email thread: new messages on it trigger future runs of this workflow.

        Idempotent — subscribing to an already-watched thread returns the existing
        subscription unchanged.
        """
        if existing := await self.find_subscription("thread", thread_id):
            return info(existing)
        await self.gmail.thread_metas(thread_id)
        return info(await insert_subscription(self.context.workflow_id, "thread", thread_id, origin="agent"))

    @tool
    async def subscribe_to_query(self, query: str) -> SubscriptionInfo:
        """Watch a Gmail search query: new matching messages trigger future runs of this workflow.

        Idempotent — subscribing to an already-watched query returns the existing
        subscription unchanged.
        """
        if existing := await self.find_subscription("query", query):
            return info(existing)
        return info(await insert_subscription(self.context.workflow_id, "query", query, origin="agent"))

    @tool
    async def unsubscribe_from_thread(self, thread_id: str) -> None:
        """Stop watching an email thread."""
        await self.remove_subscription("thread", thread_id)

    @tool
    async def unsubscribe_from_query(self, query: str) -> None:
        """Stop watching a Gmail search query."""
        await self.remove_subscription("query", query)

    @tool
    async def list_subscriptions(self) -> list[SubscriptionInfo]:
        """List this workflow's watched email threads and queries."""
        return [info(subscription) for subscription in await self.subscriptions()]

    @tool
    async def check_subscriptions(self) -> list[SubscriptionUpdate]:
        """Report new messages on this workflow's watched threads and queries, oldest first.

        Call this first whenever a run may have been triggered by email activity.
        Messages stay new until the run fired for them succeeds, so a re-fired run
        sees the same messages again; an empty list means no news.
        """
        return [
            SubscriptionUpdate(
                event=subscription.event,
                key=subscription.key,
                messages=[await self.gmail.message(meta.id) for meta in metas],
            )
            for subscription in await self.subscriptions()
            if (metas := await news_since(self.gmail, subscription))
        ]

    async def subscriptions(self) -> list[Subscription]:
        return await Subscription.find(
            Subscription.workflow_id == self.context.workflow_id, Subscription.source == "gmail"
        ).to_list()

    async def find_subscription(self, event: str, key: str) -> Subscription | None:
        return await Subscription.find_one(
            Subscription.workflow_id == self.context.workflow_id,
            Subscription.source == "gmail",
            Subscription.event == event,
            Subscription.key == key,
        )

    async def remove_subscription(self, event: str, key: str) -> None:
        match await self.find_subscription(event, key):
            case None:
                raise SubscriptionNotFound(f"not watching {event} {key!r}")
            case Subscription(origin="trigger"):
                raise SubscriptionNotFound(f"{event} {key!r} is declared by the workflow; it cannot be unsubscribed")
            case subscription:
                await subscription.delete()


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
