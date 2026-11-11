"""Surya Gates: approval gates for AI agents, across Claude and GPT."""

__version__ = "0.1.0"

from .gate import Gate, Outcome, Reply, Request, ToolCall, terminal_approver, tool_call, tool_result  # noqa: E402
from .rules import CLASSES, Verdict, classify  # noqa: E402
from .channels import text_approver  # noqa: E402

__all__ = ["CLASSES", "Gate", "Outcome", "Reply", "Request", "ToolCall", "Verdict", "classify", "terminal_approver", "text_approver", "tool_call", "tool_result", "__version__"]
