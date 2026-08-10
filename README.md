# MagnetoClip

Advanced Download Manager — **Capture the Web.**

A high-performance desktop download manager (IDM-class core capabilities with a
modern, intelligent interface) built with Python + PySide6.

## Documentation

- [Product development plan](docs/MAGNETOCLIP_PLAN.md)

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Run the application
$env:PYTHONPATH = "src"
python -m magnetoclip

# Run tests
pytest
```

## Layout

```
src/magnetoclip/
├── app/          # entrypoint, lifecycle, DI context
├── config/       # settings model
├── core/         # downloads, queues, scheduler, categories, events
├── database/     # SQLAlchemy models, repositories, migrations
├── engine/       # MagnetoCore download engine (segments, resume, retry)
├── network/      # HTTP, proxy, auth, headers
├── browser/      # browser integration / native messaging
├── media/        # media detection and ffmpeg bridge
├── security/     # credentials, validation, path safety
├── services/     # logging, filesystem, notifications, analytics
└── ui/           # PySide6 windows, widgets, themes
```
