"""Policy constants. These numbers are defined by this repository (DESIGN.md §2)."""

from __future__ import annotations

from app.models.domain import OrderStatus

AUTO_REFUND_LIMIT_CENTS = 25_000
POST_DELIVERY_REFUND_WINDOW_DAYS = 14
CANCELLABLE_ORDER_STATUSES = frozenset({OrderStatus.PAID, OrderStatus.READY_TO_SHIP})
PERMIT_TTL_SECONDS = 120
READ_MAX_ATTEMPTS = 2
