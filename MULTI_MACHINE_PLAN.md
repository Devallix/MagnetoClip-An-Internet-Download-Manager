# Multi-Machine License Binding Plan

## Overview
Update the licensing system to support configurable PC machine binding (1 to N machines per serial key). The `max_machines` column already exists in the database — we expose it through the admin UI, include machine usage data in API responses, and display it in the client UI.

## Files Modified

| # | File | Nature of Change |
|---|---|---|
| 1 | `license-server/mclip_license/app.py` | Read `max_machines` in generate, add usage counts to API responses |
| 2 | `license-server/mclip_license/templates/dashboard.html` | Add max_machines input, show usage in table, add JS sort/filter |
| 3 | `license-server/mclip_license/templates/generated.html` | Show max_machines per key |
| 4 | `src/magnetoclip/ui/dialogs/activation.py` | Show machine usage after activation |
| 5 | `src/magnetoclip/ui/pages/settings.py` | Add machine usage label in License card |
| 6 | `src/magnetoclip/ui/main_window.py` | Add machine usage to tray License info popup |
| 7 | `license-server/tests/test_api.py` | Multi-machine test coverage |

## Changes

### Change 1: Admin Generate Form — Add `max_machines` Input
**File:** `license-server/mclip_license/templates/dashboard.html`
- Add number input "Machines per key" (min 1, max 200, default 1) in the generate form grid

### Change 2: Server Generate Endpoint — Read & Set `max_machines`
**File:** `license-server/mclip_license/app.py` — `generate()` function
- Read `max_machines` from form, clamp 1–200, pass to `License(max_machines=...)`

### Change 3: Admin Dashboard — Show `max_machines` & Machine Usage in Table
**File:** `license-server/mclip_license/templates/dashboard.html`
- Header: "Machines (used / limit)"
- Data: `{{ item.activations|length }} / {{ lic.max_machines }}`

### Change 4: Admin Dashboard — Client-Side Sort & Filter
**File:** `license-server/mclip_license/templates/dashboard.html`
- Clickable "Machines" column header for ascending/descending sort
- Dropdown filter: All / 1 machine / 2 machines / 3 machines / 4+ machines
- Plain vanilla JS, no frameworks

### Change 5: Generated Keys Page — Show `max_machines`
**File:** `license-server/mclip_license/templates/generated.html`
- Show `{{ lic.max_machines }} machine(s)` alongside each serial

### Change 6: Server API Responses — Include Machine Usage
**File:** `license-server/mclip_license/app.py`
- `activate`, `validate`, `deactivate` responses include `max_machines` and `machines_used`

### Change 7: Client Activation Dialog — Show Machine Usage After Success
**File:** `src/magnetoclip/ui/dialogs/activation.py`
- Store `max_machines` and `machines_used` in settings
- Show "Activated — this key: X of Y PCs in use" before accepting

### Change 8: Client Settings Page — Show Machine Usage
**File:** `src/magnetoclip/ui/pages/settings.py`
- Add label "Machines: X of Y activated" (hidden for single-machine keys)

### Change 9: Client Tray License Info — Show Machine Usage
**File:** `src/magnetoclip/ui/main_window.py`
- Append "Machines: X of Y activated" to License info QMessageBox (when max_machines > 1)

### Change 10: Tests — Multi-Machine Scenarios
**File:** `license-server/tests/test_api.py`
- Test generate with max_machines=2 and =3
- Test activating N machines succeeds, N+1 fails
- Test API responses include max_machines and machines_used fields

## Files NOT Modified
- `db.py` — `max_machines` column already exists
- `client.py` — Already passes through all response fields transparently
- `serials.py`, `signing.py`, `state.py`, `fingerprint.py` — No changes needed
- `base.html` — No CSS changes needed
- App version and build/package — Not updated in this plan
