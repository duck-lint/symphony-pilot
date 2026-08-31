# Codex onboarding

The task launcher creates a fresh minimal CODEX_HOME containing only the six pilot role policies and generated task configuration. It never copies or symlinks the operator Codex home and never passes tracker or publication credentials to tools.

Codex runs inside the selected Linux/WSL containment backend. The current task workspace is the only source filesystem exposed to the inner policy. Model tool network is denied; App Server transport is not a task capability.

The launcher is intentionally fail-closed until the exact Codex App Server authentication path proves that hostile tools cannot recover its credential. Do not replace this gate with workspace-write, externalSandbox, a prompt instruction, or a same-user fallback.
