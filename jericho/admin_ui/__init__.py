"""
Jericho Admin UI — web-based administration panel.

The admin UI provides:
- Full access to files and knowledge objects
- Conversations viewer
- User and permissions management
- Knowledge Graph inspection and editing
- Entity merge tools
- Inbox management
- Audit log viewer
- Export capabilities
- System health and diagnostics

Served as static files by the backend or a separate frontend server.
"""

from pathlib import Path

ADMIN_UI_DIR = Path(__file__).parent
STATIC_DIR = ADMIN_UI_DIR / "static"
