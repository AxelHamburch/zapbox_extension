import pytest
from lnbits.db import Database
from sqlalchemy.ext.asyncio import create_async_engine

from .. import migrations
from ..models import ZapBox

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_m010_backfills_device_tokens(tmp_path):
    legacy = Database("ext_zapbox")
    legacy.path = str(tmp_path / "legacy.sqlite3")
    legacy.engine = create_async_engine(f"sqlite+aiosqlite:///{legacy.path}")
    try:
        async with legacy.connect() as conn:
            for name in sorted(n for n in dir(migrations) if n[:4] in {"m001", "m002", "m003", "m004", "m005", "m006", "m007", "m008", "m009"}):
                await getattr(migrations, name)(conn)
        for zapbox_id in ("a", "b"):
            await legacy.execute(
                """
                INSERT INTO zapbox.switch (id, key, title, wallet, currency, switches)
                VALUES (:id, '', 'old', 'wallet', 'sat', '[]')
                """,
                {"id": zapbox_id},
            )
        async with legacy.connect() as conn:
            await migrations.m010_device_token(conn)
            # idempotent (reinstall case)
            await migrations.m010_device_token(conn)
        boxes = await legacy.fetchall("SELECT * FROM zapbox.switch", model=ZapBox)
        tokens = {box.device_token for box in boxes}
        assert len(tokens) == 2 and all(len(t) == 32 for t in tokens)
        assert all(box.ring_login_pin_required is False for box in boxes)
    finally:
        await legacy.engine.dispose()
