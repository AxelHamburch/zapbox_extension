import asyncio

from fastapi import APIRouter
from loguru import logger

from .crud import db
from .tasks import wait_for_paid_invoices
from .views import zapbox_generic_router
from .views_api import zapbox_api_router
from .views_auth import zapbox_auth_router
from .views_lnurl import zapbox_lnurl_router

zapbox_ext: APIRouter = APIRouter(
    prefix="/zapbox", tags=["zapbox"]
)
zapbox_ext.include_router(zapbox_generic_router)
zapbox_ext.include_router(zapbox_auth_router)
zapbox_ext.include_router(zapbox_api_router)
zapbox_ext.include_router(zapbox_lnurl_router)

zapbox_static_files = [
    {
        "path": "/zapbox/static",
        "name": "zapbox_static",
    }
]
scheduled_tasks: list[asyncio.Task] = []


def zapbox_stop():
    for task in scheduled_tasks:
        try:
            task.cancel()
        except Exception as ex:
            logger.warning(ex)


def zapbox_start():
    from lnbits.tasks import create_permanent_unique_task

    task = create_permanent_unique_task("ext_zapbox", wait_for_paid_invoices)
    scheduled_tasks.append(task)


__all__ = [
    "zapbox_ext",
    "zapbox_start",
    "zapbox_static_files",
    "zapbox_stop",
    "db",
]
