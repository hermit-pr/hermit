"""Review prompt assembly."""


def build_review_prompt(
    provider: str, repo: str, ref: str, rules: str, diff: str
) -> str:
    """Assemble the complete prompt handed to opencode for a review."""
    return (
        f"You are H.E.R.M.I.T, a code reviewer. Review the {provider} change "
        f"{repo}#{ref}.\n"
        "Base your review only on the diff below.\n\n"
        f"Rules:\n{rules}\n\n"
        f"Diff:\n{diff}\n\n"
        "Write the review as a single markdown comment."
    )
