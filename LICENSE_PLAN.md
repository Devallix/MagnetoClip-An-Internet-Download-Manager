# MagnetoClip Licensing System — Implementation Plan (v1)

## Approved decisions
- Rule: **1 PC per serial key** (machine binding enforced centrally)
- Backend: Flask + SQLite license server on PythonAnywhere FREE tier ($0,
  always-on, HTTPS included). FastAPI/VPS swap possible later — API unchanged.
- Policy: **strict online validation on EVERY launch**; no offline grace
- Admin: password-protected web panel (env var + session cookie)
- Serial format: `MGCL-XXXXX-XXXXX-XXXXX-XXXXX` (ambiguity-free alphabet)
- Crypto: Ed25519-signed server responses; client embeds public key

## Architecture

```
client (httpx sync + QThread) ──HTTPS──► Flask server ──► SQLite
  keyring stores serial                    admin web panel
```

## Part 1 — license-server/
| File | Purpose |
|---|---|
| mclip_license/app.py | /v1/activate · /v1/validate · /v1/deactivate · /healthz · /admin/* |
| mclip_license/db.py | licenses(serial unique, owner_label, max_machines=1, status, created_at, expires_at∅) · activations(license_id FK, machine_id hash, hostname, app_version, activated_at, last_validated_at, UNIQUE pair) |
| mclip_license/signing.py | Ed25519 helpers, env-var private key bootstrap |
| mclip_license/serials.py | key generator (MGCL-XXXXX-XXXXX-XXXXX-XXXXX) |
| templates/admin/* | login + dashboard: list/generate/revoke/reset-binding |
| tests/test_api.py | activate ok · duplicate machine ok · 2nd PC rejected · revoked rejected · deactivate frees slot · admin auth enforced |
| requirements.txt, README.md | PythonAnywhere deploy steps |

## Part 2 — client: src/magnetoclip/services/licensing/
| File | Purpose |
|---|---|
| fingerprint.py | machine_id = SHA-256(registry MachineGuid), MAC fallback; only hash leaves PC |
| client.py | httpx activate/validate/deactivate; verifies Ed25519 signature; typed errors |
| state.py | serial → OS keyring (NOT settings DB); last_validated timestamp |

Plus: config/settings.py DEFAULTS += license.endpoint

## Part 3 — UI gating
- ui/dialogs/activation.py — modal gate: input + status line + Activate/Retry/Quit
- main.py — gate AFTER single-instance lock, BEFORE qasync loop;
  flow: no key → dialog → key → "Verifying…" QThread → fail shows reason
  (revoked/bound_to_other_pc/offline) → success continues to splash/window
- settings page — License group: masked serial, status, Deactivate this PC
- tray.py — "License…" entry next to "Open Remote…"
- Drive-by fix: main.py ~162 missing Events import (latent NameError)

## Part 4 — Tests & QA
- Client: httpx WSGI transport round-trips vs real Flask app; signature-tamper
  rejection; fingerprint stability; dialog smoke (pytest-qt)
- Manual QA: fresh activation · duplicate-PC rejection · revoke mid-flight ·
  deactivate→rebind new PC
- Version bump + zip rebuild deferred until ship

## Known limitations (accepted)
- Determined attacker can patch Python client — deters casual sharing only
- OS reinstall regenerates MachineGuid → reactivation needed
- Deployment needs user's PythonAnywhere account (steps provided)

---

## Implementation notes (as built)

- Server lives in `license-server/` with package `mclip_license` nested one level deeper than planned (`license-server/mclip_license/...`). Dev server: `python license-server/run_local.py [port=8490]`.
- **validate auto-binds when a slot is free** (self-heal). A never-activated serial validates OK and consumes the slot; only when slots are full does it return `bound_to_other_pc`. This replaced the strict `not_bound` 404 after E2E testing showed a first-launch validate would otherwise fail.
- Client default `license.endpoint = ""` means licensing disabled (dev machines keep working). Release builds must embed the production URL + Ed25519 public key into the settings defaults (and pin the public key constant in `services/licensing/client.py`).
- Gate skip hatches: empty endpoint OR env `MCLIP_LICENSE_OFF=1`.
- Serial storage: Windows Credential Manager via keyring (service `MagnetoClip`, account `license.serial`).
- Admin panel: `/admin`, password from `MCLIP_ADMIN_PASSWORD`, CSRF-protected; generate serials, revoke/re-enable, view activations (machine id, hostname, last seen).
- Deploy target unchanged: PythonAnywhere free tier (Flask WSGI) — see `license-server/README.md`.
