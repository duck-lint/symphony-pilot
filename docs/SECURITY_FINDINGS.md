# Architectural security findings register

This is the Pilot-owned living register for accepted assumptions, open seams,
and activation blockers. It records decisions rather than reproducing an audit
transcript. “Blocks Step 4” means blocks the local operator API/UI itself;
several findings still block **unattended activation** even though they do not
block this read-mostly control surface.

| Finding | Status | Importance | Affected seam | Why it matters | Intended phase | Activation consequence | Blocks Step 4? |
|---|---|---|---|---|---|---|---|
| Codex credential isolation | **OPEN** | Activation blocker | Codex App Server authentication ↔ hostile model-tool child boundary | No accepted design lets App Server authenticate while structurally preventing children from recovering reusable credentials. Current `require_execution_capability()` and `launch_codex.sh` behavior correctly fails closed; ambient Codex/OpenAI credentials must not be restored. | Step 8 | Required before unattended agent activation. | No; execution remains blocked. |
| Aggregate task storage | **OPEN** | Activation blocker | Per-task containment ↔ host storage | Per-file `RLIMIT_FSIZE` and procedural cleanup do not bound aggregate bytes or inodes; many individually bounded files can exhaust storage. Closure requires a quota-backed domain, fixed-capacity filesystem, project quota, or equivalent kernel-enforced aggregate bound. | Step 8 | Required before unattended agent activation. | No; execution remains blocked. |
| Runtime pin-to-exec TOCTOU | **OPEN** | Activation blocker | Accepted Runtime identity ↔ `Popen` executable reopen | Pilot verifies path/version/SHA and later reopens the pathname, so verified bytes are not structurally bound to executed bytes. Hashing twice is not closure; use an immutable/version-addressed reviewed artifact or equivalent binding. | Step 8 | Required before unattended agent activation. | No; execution remains blocked. |
| Branch-protection freshness | **OPEN / DEFERRED** | Publication security | Startup ruleset validation ↔ publication transaction | GitHub rulesets can change after strong startup validation. Reassert protection immediately before network publication. | Step 7 | Required for the narrowed publication authority design. | No. |
| GitHub deploy-key scope | **OPEN / DEPLOYMENT INVARIANT** | Publication security | Per-project private-key path ↔ GitHub-side registration | Source proves local key selection, not that GitHub registered its public key only for `profile.repository`. Provisioning must bind generation/registration to that repository and persist/verify GitHub key identity. | Step 7 | Must be mechanically established for publication activation. | No. |
| Changed-path scope | **POLICY-DEPENDENT / DEFERRED** | Publication policy | Licensed repository ↔ task commit contents | Publication constrains repository, branch, ancestry, exact commit, provenance, and bundle shape, but not paths within the licensed repository. No accepted promise currently requires path-level task scope. | Future policy decision; not Step 4 | No present activation consequence unless project policy adopts path scope. | No. |
| SQLite / Exqlite native boundary | **ACCEPTED ASSUMPTION** | Trust-model assumption | Host-owned SQLite bytes ↔ native SQLite/Exqlite in BEAM | Pilot SQLite bytes are trusted host-control-plane input and contained/model-controlled agents have no SQLite write authority. Native parsing therefore needs no separate process today. Revisit if those bytes become attacker/model writable. | Revisit on authority change | No current activation blocker under the stated authority boundary. | No. |
| WSL containment live proof | **IMPLEMENTED / TARGETED-VERIFIED / STATIC-REDTEAM-REVIEWED / LIVE-VERIFICATION-BLOCKED** | Activation acceptance | Windows host ↔ WSL namespaces/containment | Static review found the architecture materially sound, but actual namespace behavior remains unverified after `Wsl/EnumerateDistros/Service/E_ACCESSDENIED`. Cloud Linux evidence is not Windows↔WSL proof; do not redesign merely to evade unavailable evidence. | Step 9 / activation acceptance | Live proof remains mandatory before unattended activation. | No; execution remains blocked. |
| Runtime workflow default-branch CI drift | **OPEN** | Low / maintenance | Runtime repository default branch ↔ workflow push trigger | Runtime defaults to `master` while one workflow push trigger references `main`. Runtime is frozen and is not modified from Pilot Step 4. | Runtime maintenance | Does not govern Pilot activation, but can omit expected CI runs. | No. |
| SQLite documentation drift | **RESOLVED (Step 5)** | Documentation accuracy | Runtime capability ↔ Pilot scheduler configuration | Runtime implements the production SQLite tracker adapter and Pilot now renders it as the managed scheduler. Full lifecycle persistence remains deferred. | Step 5 | None; execution remains blocked. | No. |

## Step 5 accepted authority

GitHub Issues are not scheduler authority after Step 5. Local SQLite task rows
and `T-N` identity are scheduler authority. The Runtime scheduler reads the
host database read-only and receives no GitHub tracker credential. GitHub code
that remains in the repository is explicitly deferred publication/lifecycle
code and is not a scheduler fallback.

## Step 4 boundary

The local API is read-only and adds no credential or execution path. It does not
queue or dispatch tasks, start Runtime or Codex, publish, merge, change heads,
accept repository/path input, or alter scheduler configuration. Step 5 task
mutation is limited to the trusted host CLI. Strict outbox and output
redaction are defense in depth, not credential DLP; credential isolation must
be structural before activation. Findings above retain their status until
their named phase provides mechanical evidence sufficient to close them.
