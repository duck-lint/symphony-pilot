# Project onboarding

A registered project supplies project-owned harness inputs, repository
identity, validation rules, and bounded local settings. Adding a project must
not require a Pilot or Runtime source-code branch.

Pilot validates registration and owns the local task/lifecycle control plane.
The target repository and its harness remain authoritative for project meaning,
acceptance criteria, private inputs, and human stop conditions. Repository
service observations may support host materialization and publication, but do
not become local scheduler authority.

Do not add external service metadata or browser state as lifecycle inputs. Do
not add project-specific production conditionals. The
canary is an ordinary registered test project, not infrastructure.

The supervised-local substrate is proven; canonical fresh-specialist
orchestration and unattended hardening are not claimed live from registration
or policy availability.
