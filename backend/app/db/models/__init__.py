"""Every mapped model, imported here so the metadata is always complete.

SQLAlchemy resolves ``ForeignKey("profiles.id")`` lazily, by looking the table up in
the shared metadata. If a process imports `book` but never `profile`, that lookup
fails with NoReferencedTableError the first time a mapper is configured — which is
how the API worked and the Celery worker did not: the API imports `profile` on its
auth path by accident, and the worker had no reason to.

Importing them from one place makes "which models are loaded" stop depending on
which code path happened to run first.
"""

from app.db.models.answer import Answer
from app.db.models.assessment import Assessment
from app.db.models.attempt import Attempt
from app.db.models.book import Book, Chapter, Chunk
from app.db.models.chat import ChatMessage, ChatSession
from app.db.models.feedback import ContentFeedback
from app.db.models.notification import Notification
from app.db.models.proctor import ProctorEvent, ProctorSession
from app.db.models.profile import Profile
from app.db.models.question import Question, QuestionKey, QuestionSit

__all__ = [
    "Answer",
    "Assessment",
    "Attempt",
    "Book",
    "Chapter",
    "ChatMessage",
    "ChatSession",
    "Chunk",
    "ContentFeedback",
    "Notification",
    "ProctorEvent",
    "ProctorSession",
    "Profile",
    "Question",
    "QuestionKey",
    "QuestionSit",
]
