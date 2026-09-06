# Step 8 disposable target-twin runner

Tier C is intentionally unavailable until a dedicated disposable Windows
runner is provisioned. The runner is not the human workstation and must not
contain production credentials, production VHDX state, or production SQLite
state.

The runner must satisfy the exact contract in
`host-contract/symphony-target-v1.json` and register with all five labels:

`self-hosted`, `windows`, `x64`, `symphony-target`, `symphony-disposable`.

Its bootstrap must install Windows build `10.0.22631.6199`, PowerShell Core
`7.6.5`, WSL `2.7.12.0`, the Microsoft WSL2 kernel package
`6.18.33.2-2`, Ubuntu-24.04 as WSL2, and the `duck-lint` user. The Ubuntu
userspace/toolchain must satisfy the compatible buildability tier: Ubuntu
24.04.x x86_64, `/usr/bin/cc` GCC 13, glibc 2.39 family, e2fsprogs 1.47
family, util-linux at least 2.39, and `__NR_quotactl_fd` 443.

The machine must have enough disposable disk capacity for the exact 64-GiB
fixed VHDX plus temporary deployment and test state. If nested virtualization
is used, WSL2 nested virtualization must be enabled. The runner must create
`C:\SymphonyTargetRunner\runner-marker.json` with schema
`symphony-pilot-disposable-target-runner/v1` and `disposable: true`.

The marker, labels, exact host verification, and trusted repository/ref guard
are all required before fixture mutation. The target-twin workflow does not
run for fork pull requests. After each run the runner must be reset or
reimaged; it is never a production host and is not a generic privileged
command broker.

External infrastructure still required: one dedicated disposable
self-hosted Windows runner with the labels and exact target capability above.
