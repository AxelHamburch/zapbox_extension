from lnbits.db import Database

db = Database("ext_zapbox")


async def m001_initial(db):
    """
    Initial zapbox table.
    """
    await db.execute(f"""
        CREATE TABLE zapbox.switch (
            id TEXT NOT NULL PRIMARY KEY,
            key TEXT NOT NULL,
            title TEXT NOT NULL,
            wallet TEXT NOT NULL,
            currency TEXT NOT NULL,
            switches TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT {db.timestamp_now},
            updated_at TIMESTAMP NOT NULL DEFAULT {db.timestamp_now}
        );
    """)
    await db.execute(f"""
        CREATE TABLE zapbox.payment (
            id TEXT NOT NULL PRIMARY KEY,
            zapbox_id TEXT NOT NULL,
            payment_hash TEXT,
            payload TEXT NOT NULL,
            pin INT,
            sats {db.big_int},
            created_at TIMESTAMP NOT NULL DEFAULT {db.timestamp_now},
            updated_at TIMESTAMP NOT NULL DEFAULT {db.timestamp_now}
        );
    """)


async def m002_add_password(db):
    await db.execute("""
        ALTER TABLE zapbox.switch
        ADD COLUMN password TEXT;
        """)


async def m003_disabled(db):
    await db.execute("""
        ALTER TABLE zapbox.switch
        ADD COLUMN disabled BOOLEAN NOT NULL DEFAULT FALSE;
        """)


async def m004_disposable(db):
    await db.execute("""
        ALTER TABLE zapbox.switch
        ADD COLUMN disposable BOOLEAN NOT NULL DEFAULT TRUE;
        """)


async def m005_minipos_payment(db):
    """
    Mini-PoS payments: invoices created from amounts entered on the device
    touch display. Tracked separately from switch payments so the device can
    query the last settled amount ("Last Pay" button).
    """
    await db.execute(f"""
        CREATE TABLE zapbox.minipos_payment (
            id TEXT NOT NULL PRIMARY KEY,
            zapbox_id TEXT NOT NULL,
            wallet TEXT NOT NULL,
            sats {db.big_int},
            amount REAL NOT NULL,
            currency TEXT NOT NULL,
            bolt11 TEXT NOT NULL DEFAULT '',
            paid BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMP NOT NULL DEFAULT {db.timestamp_now},
            updated_at TIMESTAMP NOT NULL DEFAULT {db.timestamp_now}
        );
    """)


async def m006_auth_keys(db):
    """
    LNURL-auth (LUD-04) identities. Each row is a domain-specific linking key
    (LUD-05) that a known wallet proved ownership of. When such a key
    authenticates (action=auth) the device triggers its relay — analogous to a
    settled payment, but without any payment.

    Uniqueness of (zapbox_id, pubkey) is enforced with an inline table
    constraint (creates an implicit index) rather than a separate CREATE INDEX:
    a schema-qualified `CREATE INDEX ... ON zapbox.auth_key` is a syntax error
    on SQLite, and the inline form is portable across SQLite and PostgreSQL.
    The DROP IF EXISTS recovers from an earlier partial install where the table
    was created but the follow-up statement failed (the table is empty here).
    """
    await db.execute("DROP TABLE IF EXISTS zapbox.auth_key;")
    await db.execute(f"""
        CREATE TABLE zapbox.auth_key (
            id TEXT NOT NULL PRIMARY KEY,
            zapbox_id TEXT NOT NULL,
            pubkey TEXT NOT NULL,
            label TEXT,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP NOT NULL DEFAULT {db.timestamp_now},
            UNIQUE (zapbox_id, pubkey)
        );
    """)


async def m007_teach_pin_and_touch_enabled(db):
    """
    Teach-mode access control on the ZapBox instance:
    - teach_pin: 6-digit PIN verified server-side when opening a teach session.
    - touch_enabled: gate for the teach access. Set to false after 3 wrong PIN
      attempts (locks enrolling new identities); the operator re-enables it in
      the instance editor. Normal payment/touch operation is unaffected.
    """
    await db.execute("""
        ALTER TABLE zapbox.switch
        ADD COLUMN teach_pin TEXT;
        """)
    await db.execute("""
        ALTER TABLE zapbox.switch
        ADD COLUMN touch_enabled BOOLEAN NOT NULL DEFAULT TRUE;
        """)
