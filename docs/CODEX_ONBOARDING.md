# Codex onboarding

The launcher creates a fresh task-local `CODEX_HOME` containing only the six
pilot role policies and generated task configuration. It never copies or
symlinks the operator home and never passes tracker or publication credentials
to tools. Role TOML files are an allowlist; their presence does not prove
native role dispatch or read-only enforcement.

Before any App Server start, the launcher runs the synthetic hostile task-domain
fixture and then the exact execution-capability gate. The fixture proves the
Linux/WSL namespace constructor. The second gate currently returns
`codex_auth_boundary`; no Codex process is started.

Do not mount operator `auth.json`, pass `CODEX_HOME`, inject readable API keys,
grant tool network, or fall back to same-user execution. The current official
App Server exposes authentication/configuration surfaces and supports the
`externalSandbox` protocol representation, but those representations are not
alone proof that hostile model tools cannot recover host authority.
