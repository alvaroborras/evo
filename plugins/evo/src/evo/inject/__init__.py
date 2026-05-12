"""Cross-host directive delivery — auto-register, queues, marker, drain.

See `notes/cross-host-inject-design.md` for the full design.

Public surface:
    - paths.directives_dir(root): canonical inject root under workspace
    - registry.register_session(root, session_id, host, exp_id=None): idempotent
    - queue.append_event(root, kind, **fields): writes to workspace.jsonl
    - queue.append_exp_event(root, exp_id, kind, **fields): writes to exp queue
    - marker.touch(root, session_id) / marker.exists(root, session_id) / unlink
    - drain.drain_session(root, session_id, host): the actual hook payload generator
"""

from __future__ import annotations
