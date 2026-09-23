"""Read-only investigation tools. See DESIGN.md for the tool contract."""

from investigator.tools.base import ToolContext, ToolResult, ToolRegistry
from investigator.tools.catalog import build_registry

__all__ = ["ToolContext", "ToolResult", "ToolRegistry", "build_registry"]
