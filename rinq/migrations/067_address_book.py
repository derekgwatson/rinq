"""Add address_book table for tenant-scoped external contacts.

All tenants get an address book. Entries can be added manually or
auto-synced from an external source (e.g. Peter for Watson). Source-
synced rows are identified by source + external_id and are upserted
on each sync run; manually-added rows have source='manual'.

mobile_e164 is the normalised E.164 number for fast inbound caller-ID
lookup (indexed). display_mobile stores the original formatting.
"""


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS address_book (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            display_mobile TEXT,
            mobile_e164 TEXT,
            section TEXT,
            position TEXT,

            -- 'manual' or the sync source name (e.g. 'peter')
            source TEXT NOT NULL DEFAULT 'manual',
            -- ID in the external system (NULL for manual entries)
            external_id TEXT,

            synced_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),

            UNIQUE(source, external_id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_address_book_mobile
            ON address_book(mobile_e164)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_address_book_source
            ON address_book(source)
    """)


def down(conn):
    conn.execute("DROP TABLE IF EXISTS address_book")
