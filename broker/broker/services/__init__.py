"""Service layer: agent-facing (agent.py) and owner-facing (admin.py,
plugins_admin.py) operations. Routers are thin wrappers over these.

agent.py must never import admin.py or identity/: a test walks the import
graph to prove agents have no path to approvals."""
