# #46 Windows AppContainer shell boundary verification

Scope: Windows host, current workspace, synthetic files, local listeners; no external network service or real credential used. Command `python -m pytest tests/test_shell_memory_isolation.py -q -rs` → **14 passed, 0 failed**; `python -m pytest -q -rs` → **197 passed, 1 skipped** (host cannot create file symlinks; the junction/reparse integration test passed). Actual tests—not a proof for all configurations—cover:

- Allowed workspace write; blocked outside-workspace and protected state read/write and SQLite/rename attempts.
- Local loopback and host non-loopback LAN TCP connection attempts failed. No test connected to an external service; other transports/destinations remain unverified.
- Existing workspace junction/reparse point and hardlink reject before command execution; job-bound child processes cannot survive cleanup; injected initialization failure has no unsandboxed fallback.
- A deterministic hardlink inserted **after the first tree scan** used to be missed by inherited workspace ACL grant; a second scan after granting now rejects it. Windows ACL security handles open reparse entries without following them and reject reparse/hardlink handles before modifying DACL. Removing the new scan fails `test_link_swapped_after_scan_before_acl_grant_is_rejected`.

Limits: a tree scan or pre-launch check cannot eliminate races after the last scan or stop another process altering a workspace during execution. The AppContainer ACL/job boundary is the enforcement boundary; this test set does not formally prove every filesystem redirection, IPC/network channel or hardware failure. The project deliberately permits `.env` inside the workspace under ADR 0016; workspace credentials are not isolated from `bash`. Do not describe these tests as a general guarantee of secret confinement or race freedom.
