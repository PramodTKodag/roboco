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
