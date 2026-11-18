"""Echo Mesh node runtime (Phase 1). Standard library only."""
VERSION = "0.1.0"
COORD_PORT = 47821     # nodes -> coordinator (TLS, outbound from nodes only)
ADMIN_PORT = 47830     # admin API, 127.0.0.1 only
BEACON_PORT = 47820    # UDP discovery
