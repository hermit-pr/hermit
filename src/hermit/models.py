"""Shared data models used across the bot."""

from typing import Any, Literal, Optional

from pydantic import BaseModel, model_validator

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
    pr_title: str = ""
    pr_body: str = ""
    url: str = ""
    project_id: Optional[int] = None
    source_repo: str = ""

    @model_validator(mode="before")
    @classmethod
    def _coerce_none_str_to_empty(cls, data: Any) -> Any:
        """Replace None with '' for every optional-string field so that
        parsers that accidentally pass None do not cause ValidationError."""
        if isinstance(data, dict):
            for field in (
                "pr_title",
                "pr_body",
                "url",
                "head_sha",
                "head_ref",
                "base_sha",
                "base_ref",
                "source_repo",
            ):
                if data.get(field) is None:
                    data[field] = ""
        return data
