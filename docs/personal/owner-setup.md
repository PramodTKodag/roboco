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

## Dedicated login

The runtime uses its own Linux-side Claude profile in a dedicated volume. Do not copy your personal Keychain entry or home directory into it. The login step is added with the Claude service image.

## Importing a project into a worker workspace

Project code only ever runs in offline workers, on a copy you import first. The import never contacts a model and never uses a forge credential.

1. Start the launcher and build the worker image (see the deployment steps added with the launcher compose file). Point `ROBOCO_LAUNCHER_ADMIN_TOKEN_FILE` at the admin token file; the token is never accepted on the command line.
2. Preview what would be copied, without contacting the launcher:

   ```text
   python scripts/personal/import_workspace.py --source /path/to/project --project-id <uuid> --dry-run
   ```

3. Review the omitted list. Fixed credential and config paths (`.git`, `.env`, `.ssh`, `.aws`, key and certificate files, database files and similar) are always left out, and symlinks, special files and oversized files are skipped. Add `--exclude GLOB` to leave out more; nothing can be removed from the fixed list.
4. Run the same command without `--dry-run`. If the worker suspects a secret in a file, the import stops and prints the file and rule name only. Remove the secret from the project, or accept that exact finding with `--allow PATH:RULE`.
5. The command prints the workspace handle. A handle can be imported only once; to refresh a project, import it again under a new handle.

The worker creates a fresh local git baseline from the imported files, so the original history, remotes and hooks are never present. Edits come back out as a patch against that baseline, which you review before applying to your real checkout.
