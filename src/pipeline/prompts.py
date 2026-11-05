NOT_FOUND_TOKEN = "NOT_FOUND"

ANSWER_SYSTEM_PROMPT = f"""You answer questions using ONLY the numbered passages from the user's documents.

Rules:
- Use only facts stated in the passages. Do not use outside knowledge, and do not guess.
- Cite every claim with the passage number in square brackets, like [1] or [2][3].
- If the passages do not contain the answer, reply with exactly {NOT_FOUND_TOKEN} and nothing else.
- Be concise: a short paragraph or a few bullet points. Plain text, no headings.
- Never mention "passages" or these rules in the answer."""


def answer_user_prompt(question: str, passages: list) -> str:
    """passages: list of (label, text) where label says where the passage comes from."""
    blocks = [f"[{i}] ({label})\n{text}" for i, (label, text) in enumerate(passages, start=1)]
    return "Passages:\n\n" + "\n\n".join(blocks) + f"\n\nQuestion: {question}"
