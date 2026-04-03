---
name: zapbox-extension-lnbits
description: "ZapBox LNbits Extension — Lightning payment control for ZapBox hardware. Stack: Python 3.11+, FastAPI, Pydantic, async SQLite, Vue 3 + Quasar, WebSocket. Extends LNbits with device management, LNURL-pay, and NFC Bolt Card support."
---

# ZapBox LNbits Extension

## Quick Start

### Build & Test
```bash
# Format code (black, ruff, prettier)
make format

# Type check & lint
make check

# Run tests
make test

# All in one
make format check test
```

### Local Development (within LNbits)
```bash
# Install LNbits + extension dependencies
pip install -e .

# Start LNbits dev server (auto-reloads extension changes)
lnbits --reload

# Available at: http://localhost:5000
```

## Project Structure

```
zapbox_extension/
├── __init__.py              # Router registration & lifecycle (start/stop)
├── models.py                # Pydantic data models (ZapBox, Switch, Device config)
├── views.py                 # Generic HTML routes & template rendering
├── views_api.py             # REST API endpoints (core business logic)
├── views_lnurl.py           # LNURL-pay & callback handlers
├── crud.py                  # Database operations (select, insert, update)
├── tasks.py                 # Background: invoice listener, payment processor
├── config.json              # GitHub repo reference (org/repo metadata)
├── manifest.json            # Registry metadata (version, contributors, links)
├── package.json             # Node.js dev tools (prettier, pyright)
├── pyproject.toml           # Python dependencies & tool config
├── Makefile                 # Build targets (format, check, test)
├── static/
│   ├── index.vue            # Admin dashboard (device list, config)
│   ├── index.js             # Dashboard component logic & API calls
│   ├── public.vue           # Public customer UI (payment status)
│   ├── public.js            # Public component logic
│   ├── routes.json          # Frontend SPA routes
│   └── image/               # Icons & extension images
└── tests/
    ├── __init__.py
    └── test_init.py         # Extension lifecycle tests
```

## Architecture Patterns

### 3-Layer Design
```
┌──────────────────────────────────────────────┐
│  Frontend (Vue + Quasar)                     │
│  /zapbox/static/index.vue + public.vue       │
└────────────────┬─────────────────────────────┘
                 │ HTTP/WebSocket
┌────────────────▼─────────────────────────────┐
│  API Layer (FastAPI)                         │
│  /zapbox/api/v1/* endpoints (views_api.py)   │
│  /zapbox/lnurl/* handlers (views_lnurl.py)   │
└────────────────┬─────────────────────────────┘
                 │ async
┌────────────────▼─────────────────────────────┐
│  Data Layer (Pydantic + Async SQLite)        │
│  models.py (schemas)                         │
│  crud.py (database operations)               │
│  tasks.py (invoice listeners)                │
└──────────────────────────────────────────────┘
```

### Database Pattern
```python
from lnbits.db import Database

db = Database("ext_zapbox")

# Async operations
async def get_device(device_id: str):
    return await db.fetchone(
        "SELECT * FROM zapbox.devices WHERE id = ?",
        (device_id,),
        model=Device  # Auto-deserialize to Pydantic
    )

async def create_device(data: CreateDevice):
    device_id = urlsafe_short_hash()
    await db.execute(
        "INSERT INTO zapbox.devices (id, name, lnbits_key) VALUES (?, ?, ?)",
        (device_id, data.name, data.lnbits_key)
    )
    return device_id
```

### API Endpoint Pattern
```python
from fastapi import APIRouter, Depends, HTTPException
from lnbits.decorators import require_admin_key

router = APIRouter()

@router.post("/api/v1/devices")
async def create_device(
    data: CreateDevice,
    _=Depends(require_admin_key)  # Admin authentication
):
    try:
        device_id = await crud.create_device(data)
        return {"device_id": device_id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
```

### Background Invoice Listener Pattern
```python
# tasks.py
async def wait_for_paid_invoices():
    while True:
        # Poll LNbits invoices periodically
        invoices = await fetch_pending_invoices()
        for inv in invoices:
            if inv.paid:
                await on_invoice_paid(inv)  # Trigger business logic
        await asyncio.sleep(1)

# __init__.py — Register on startup
async def zapbox_start():
    await scheduled_tasks.add_task("wait_for_paid_invoices", wait_for_paid_invoices)

async def zapbox_stop():
    await scheduled_tasks.cancel_all_tasks_from_extension("zapbox")
```

## Development Conventions

### Naming
- **Extension ID**: lowercase, matches folder name (`zapbox`)
- **Database tables**: prefixed with extension name (`zapbox.devices`, `zapbox.switches`)
- **API routes**: `/zapbox/api/v1/<resource>`, `/zapbox/lnurl/<action>`
- **Vue components**: PascalCase for components, camelCase for methods/props

### Type Hints
```python
# models.py — All Pydantic models
class Device(BaseModel):
    id: str
    name: str
    active: bool = True
    created_at: datetime

class CreateDevice(BaseModel):
    name: str
    description: Optional[str] = None
```

### Error Handling
```python
# views_api.py
try:
    device = await crud.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device
except ValueError as e:
    raise HTTPException(status_code=400, detail=f"Invalid request: {e}")
except Exception as e:
    LOG.error(f"Unexpected error: {e}")
    raise HTTPException(status_code=500, detail="Internal server error")
```

### Code Quality Tools
- **Format**: `black` (Python) + `prettier` (JavaScript/Vue)
- **Type check**: `mypy` + `pyright`
- **Lint**: `ruff` (Python)
- **Manager**: `uv` (dependency resolver)

Run all checks:
```bash
make format check
```

## Dependency Injection

LNbits provides decorators for authentication:

```python
from lnbits.decorators import (
    require_admin_key,      # User-scoped admin key (extension-specific)
    require_invoice_key,    # Invoice-scoped key (payment access only)
    check_user_exists       # Verify user is logged in
)

# Only admin of this device can update it
@router.put("/api/v1/devices/{device_id}")
async def update_device(
    device_id: str,
    data: UpdateDevice,
    user=Depends(check_user_exists)
):
    device = await crud.get_device(device_id)
    if device.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized")
    # ... update logic
```

## Frontend (Vue + Quasar)

### Admin Dashboard (`static/index.vue`)
- Device list (q-table with search, pagination)
- Device config form (name, description, settings)
- Switch/relay control UI
- Export/import functionality

### Public Payment Page (`static/public.vue`)
- Payment status indicator
- QR code display (optional)
- Transaction history

### Routes (`static/routes.json`)
```json
{
  "name": "zapbox",
  "routes": [
    {
      "path": "/",
      "component": "Dashboard"
    },
    {
      "path": "/devices/:id",
      "component": "DeviceDetail"
    }
  ]
}
```

API calls use window-level mixins (provided by LNbits):
```javascript
// static/index.js
export default {
  methods: {
    async fetchDevices() {
      const { data } = await this.$axios.get("/zapbox/api/v1/devices");
      this.devices = data;
    }
  }
}
```

## LNURL Integration

ZapBox Extension provides LNURL-pay callbacks for Lightning payments:

```python
# views_lnurl.py
@router.get("/lnurl/{device_key}")
async def lnurl_handler(device_key: str):
    # Generate LNbits invoice linked to this device
    invoice = await create_invoice(device_key, amount)
    return {
        "tag": "payRequest",
        "callback": f"https://server/zapbox/lnurl/{device_key}/callback",
        "minSendable": 1000,
        "maxSendable": 100000000,
        "metadata": json.dumps([["text/plain", f"Pay to {device.name}"]])
    }

@router.get("/lnurl/{device_key}/callback")
async def lnurl_callback(device_key: str, pr: str):
    # Return QR code with payment request
    return {"pr": pr}
```

Backend (`tasks.py`) listens for "paid" events via WebSocket and triggers relay action.

## Configuration Files

### manifest.json
```json
{
  "id": "zapbox",
  "name": "ZapBox — Lightning Device Controller",
  "version": "1.0.0",
  "min_lnbits_version": "0.14.0",
  "repository": "https://github.com/AxelHamburch/zapbox_extension",
  "contributors": [...]
}
```

### config.json
```json
{
  "org": "AxelHamburch",
  "repo": "zapbox_extension"
}
```

## Database Schema

Extensions auto-create tables on startup. Define via `__init__.py`:

```python
async def zapbox_start():
    await db.execute("""
        CREATE TABLE IF NOT EXISTS zapbox.devices (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT, 
            active BOOLEAN DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
```

## Common Issues & Fixes

| Issue | Fix |
|-------|-----|
| **Import error: `lnbits` module not found** | Run `pip install -e .` in LNbits root to install dev mode |
| **Frontend routes not showing** | Verify `routes.json` is in `static/` and referenced in `__init__.py` |
| **Database table not created** | Check `zapbox_start()` function; run it manually in Python REPL |
| **Type checker complains about Pydantic** | Ensure `pyright` config includes `.pyright.json` with `lnbits` stubs |
| **Tests fail with import errors** | Run `make check` first to identify missing deps |

## Testing

Run test suite:
```bash
make test
PYTHONUNBUFFERED=1 DEBUG=true uv run pytest
```

Test file: `tests/test_init.py`
- Extension lifecycle (start/stop)
- Basic API endpoint validation
- Database operations

## Related Docs

- [LNbits Extension Guide](https://docs.lnbits.org/guide/extensions.html)
- [ZapBox Firmware](../ZapBox/) — Embedded device controller
- [LNbits Core](../lnbits-1/) — Main server
- [Boltcards Extension](../boltcards/) — Similar extension architecture reference

