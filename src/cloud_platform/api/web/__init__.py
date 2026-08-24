"""Server-rendered web panels (M14-004/M14-005)."""

from .admin import router as admin_web_router
from .customer import router as customer_web_router

__all__ = ["admin_web_router", "customer_web_router"]
