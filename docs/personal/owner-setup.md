# Owner setup (subscription account)

Steps only the account owner can do. The software cannot perform them and cannot verify them.

## Before the first real run: turn off paid overage on the account

Signing in with a subscription does not by itself stop separately billed usage. The CLI's auth status reports the sign-in method, not the account's billing settings, so a clean status never proves that no paid overage can happen.

1. Open the Claude account settings for the account used by the dedicated runtime profile.
2. Turn off extra usage / usage credits, and turn off automatic top-ups, wherever the account exposes them.
3. Be aware these are account-level: the change also applies to your other Claude sessions on the same account.
4. Do not pay for or enable anything on the tool's behalf; the tool never will.

## Local acknowledgement (not proof)

After you have done the above, record a local acknowledgement. It stores only a timestamp under the setting `subscription_billing_acknowledged_at`. It means "the owner said this was done". It is not provider-enforced and is not evidence of the real billing state. Do not store screenshots or account details anywhere in the project.

## Bring the stack up

Run every command from a shell that has no provider credentials set. The tool refuses to continue, and names the variable (never its value), when `ANTHROPIC_*`, `CLAUDE_CODE_*` or similar variables are present; start a clean shell with `env -i PATH="$PATH" HOME="$HOME" make personal-up` if yours exports them.

1. `make personal-stage` fetches the pinned official CLI and verifies both digests (see `cli-contract.md`). It needs no credentials.
2. `make personal-up` generates private state on first use under `.personal/` (gitignored, owner-only permissions: database password, encryption key, internal tokens and the panel login), builds the pinned images, and starts every service. State is never rotated by a restart.
3. The panel is at `http://localhost:3300` (or `http://127.0.0.1:3300`). Change the port with `ROBOCO_PERSONAL_PORT`; hosts and allowed origins follow it automatically. Only the edge is published, and only on loopback.
4. Sign in to the panel with the credentials in `.personal/panel-login.txt`. Safari rejects `Secure` cookies on plain `http://localhost`; use Chrome, Firefox or Edge.
5. `make personal-status` shows service health and the subscription state (`login_required` until you complete the next step).
6. `make personal-stop` stops the stack. It never deletes a volume, the subscription login or a workspace.

## Dedicated login

The runtime uses its own Linux-side Claude profile in a dedicated volume. Do not copy your personal Keychain entry or home directory into it. `make personal-login` runs the official sign-in inside the trusted model service container, from your terminal only; no task or model tool can start it.

The sign-in needs a few vendor endpoints the normal egress profile does not allow. The **maintenance** profile in `deploy/personal/egress-policy.json` is the reviewed list for that window, and it ships **empty**, so everything is refused until you approve entries. The flow:

1. Run `make personal-login`. Model runs pause and the proxy switches to the maintenance profile for the duration.
2. The login is expected to fail the first time. When it ends, the tool prints each destination the proxy refused (host, port and path only; never a query, header or body).
3. Review each destination. Approve only what the official sign-in needs by adding an exact entry (host, port, methods, path, content types) to the `maintenance` list, then update `deploy/personal/egress-policy.sha256` with the new digest. The proxy refuses to start on a digest mismatch, by design.
4. Repeat until the login completes. The normal profile is restored automatically, even if the login fails.
5. Confirm with `make personal-status` that the state is no longer `login_required`.

Everything that goes to the vendor, including every imported file the model reads, is sent to Anthropic under your account's terms.

## Importing a project into a worker workspace

Project code only ever runs in offline workers, on a copy you import first. The import never contacts a model and never uses a forge credential.

1. Create the project in the panel (or the API) and note its id.
2. Preview what would be copied, without contacting the launcher:

   ```text
   make personal-import PROJECT=<project uuid> SOURCE=/path/to/project DRY_RUN=1
   ```

3. Review the omitted list. Fixed credential and config paths (`.git`, `.env`, `.ssh`, `.aws`, key and certificate files, database files and similar) are always left out, and symlinks, special files and oversized files are skipped. The tool also refuses your home directory and any parent of it. For more exclusions, run `scripts/personal/stack.py import` with `--exclude GLOB`; nothing can be removed from the fixed list.
4. Run the same command without `DRY_RUN=1`. The directory is mounted read-only into a one-off container, the offline worker admits the archive, and the new workspace is linked to the project. If the worker suspects a secret in a file, the import stops and prints the file and rule name only. Remove the secret from the project, or accept that exact finding with `--allow PATH:RULE`.
5. A workspace handle can be imported only once; to refresh a project, import it again under a new handle.

The worker creates a fresh local git baseline from the imported files, so the original history, remotes and hooks are never present.

## Taking changes back out

Review is a local patch, never a pull request. After a task is accepted, export its patches with `make personal-export TASK=<task id> OUT=<directory>`, review them, and apply them to your real checkout yourself. Patches are capped at 64 KiB each, and a patch that looks like it contains a secret is blocked.

## Known limits to keep in mind

- The worker image has no project toolchains yet, so project test and build commands that need one cannot run.
- The runtime is verified on OrbStack with Docker; Docker Desktop and the gVisor (`runsc`) option are untested.
- The controller secrets live in a gitignored owner-only file and are visible through `docker inspect` to anyone who can run docker on this machine.
- The launcher holds the docker socket and runs as root; it is reachable only from the controllers on an internal network, with two distinct bearer tokens.
- The live subscription sign-in method value and the vendor endpoint list are unverified until you complete a login.
