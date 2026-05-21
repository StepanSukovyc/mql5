"""Helpers for identifying which strategy owns an MT5 position or order."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


DEFAULT_PRIMARY_STRATEGY_ID = "gemini_primary"
DEFAULT_PRIMARY_MAGIC = 234000
DEFAULT_INDEX_STRATEGY_ID = "gemini_indices"
DEFAULT_INDEX_MAGIC = 234100


@dataclass(frozen=True)
class StrategyContext:
	strategy_id: str
	magic: int
	order_comment: str


def build_strategy_context(strategy_id: Optional[str] = None, magic: Optional[int] = None) -> StrategyContext:
	"""Return a normalized strategy context for MT5 orders and positions."""
	resolved_strategy_id = (strategy_id or DEFAULT_PRIMARY_STRATEGY_ID).strip() or DEFAULT_PRIMARY_STRATEGY_ID
	if magic is None:
		resolved_magic = DEFAULT_INDEX_MAGIC if resolved_strategy_id == DEFAULT_INDEX_STRATEGY_ID else DEFAULT_PRIMARY_MAGIC
	else:
		resolved_magic = int(magic)

	return StrategyContext(
		strategy_id=resolved_strategy_id,
		magic=resolved_magic,
		order_comment=f"ga:{resolved_strategy_id}",
	)


def position_belongs_to_strategy(
	position: Any,
	*,
	strategy_id: Optional[str] = None,
	magic: Optional[int] = None,
	allow_legacy: bool = False,
) -> bool:
	"""Return True when an MT5 position belongs to the requested strategy context."""
	context = build_strategy_context(strategy_id=strategy_id, magic=magic)
	position_magic = getattr(position, "magic", None)
	if position_magic == context.magic:
		return True

	position_comment = str(getattr(position, "comment", "") or "")
	if context.strategy_id and context.strategy_id in position_comment:
		return True

	if not allow_legacy:
		return False

	if int(position_magic or 0) != 0:
		return False

	if DEFAULT_PRIMARY_STRATEGY_ID in position_comment or DEFAULT_INDEX_STRATEGY_ID in position_comment:
		return False

	return True