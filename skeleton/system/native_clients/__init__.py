"""Distribution of native client updates from the core's own server (#606).

The operator declares, in a private JSON file (``SKELETON_NATIVE_CLIENTS_CONFIG``), where the
signed releases of each native client come from (a GitHub repository, with a read token), the
channels and the publisher of the operator's trust store. The sync (command and periodic job)
downloads each release's Velopack feed (``releases.<os>.json``), its Ed25519 envelope
(``releases.<os>.sig.json``) and the files the feed lists; it verifies the envelope with the
existing primitives (canonical JSON, trust store), then the SHA-256 of every file, and only then
publishes the feed on its channel. Nothing is served before it is verified, and the bytes are
served exactly as verified, anonymously: an old client must update even when it can no longer
sign in. The content carries no personal data.
"""
