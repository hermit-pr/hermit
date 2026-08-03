"""Shared data models used across the bot."""

from typing import Literal, Optional

from pydantic import BaseModel

Provider = Literal["github", "gitlab"]


class ChangeEvent(BaseModel):
    """A normalized description of a pull/merge request event.

    The same shape is produced for GitHub pull requests and GitLab merge
    requests so the rest of the bot does not care which provider fired.
    """

    provider: Provider
    action: str
    repo: str
    ref: str
    head_sha: str = ""
    head_ref: str = ""
    base_sha: str = ""
    base_ref: str = ""
    title: str = ""
    url: str = ""
    project_id: Optional[int] = None
