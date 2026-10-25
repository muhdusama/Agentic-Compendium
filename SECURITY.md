# Security policy

Agentic Compendium is a single-owner, self-hosted app intended for access over a trusted LAN or VPN using HTTPS. Do not expose the Python backend port or its SQLite data directory directly to the public Internet.

## Reporting a vulnerability

Use GitHub's **Security** tab and **Report a vulnerability** when private vulnerability reporting is available. If that option is not enabled, open an issue requesting a secure contact channel **without disclosing any vulnerability details**. Never post exploits, credentials or personal data publicly.

Include affected version or commit, the relevant endpoint or component, steps to reproduce using disposable data, and the potential impact. Do not test against another person's instance.

## Deploying safely

- Use three different, randomly generated owner, read and publish credentials in a private `.env` file.
- Use an HTTPS reverse proxy and restrict app access to your LAN or VPN.
- Restrict filesystem access to `data/`, SQLite backups and downloaded archives. They can contain Hermes credentials and personal content.
- Avoid shared devices. Plain sign-out keeps offline IndexedDB content; use **Sign out and forget this device** to delete it.
- Apply updates and keep backups of your private database outside the Git repository.

The repository is source code only. It must not contain production configuration, internal hostnames, personal deliveries or private backups.
