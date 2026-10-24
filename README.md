<p align="center"><img src="server/web/icon-192.png" width="90" height="90" alt="Agentic Compendium logo"></p>

# Agentic Compendium

**A self-hosted, responsive PWA for organising Hermes cron-job results and personal notes.**

Agentic Compendium is a single-container application with a Python standard-library HTTP server, persistent SQLite and an offline-capable browser client. It works on phones, tablets and desktops without a commercial Apple Developer account. It is designed to run privately behind your existing LAN or VPN HTTPS reverse proxy.

## Features

- **Channels and sections:** Create sections, drag and drop channels, reorder them and move them between sections. Touch-friendly Move Up, Move Down and section selection controls are also available.
- **Rich reading:** Read Hermes deliveries grouped by channel and day, with formatted Markdown headings, lists, tables, links and code. Export Rich Text Format (`.rtf`) documents.
- **Organise safely:** Search, multi-select, move, mark read/unread, send items to Bin and restore them without deleting source deliveries or notes.
- **Personalise:** System, Light and Dark modes, five accent colours, collapsible desktop/tablet sidebars, and responsive phone navigation.
- **Private multi-device sync:** Incremental SQLite change cursors, optimistic revisions, conflict notices, tombstones, offline IndexedDB cache and queued typed notes.
- **Hermes integration:** Optional dashboard connection and selective import of completed cron runs; Hermes remains responsible for schedules and execution.
- **Native archives:** Import a portable export from a native Agentic Compendium App installation. Original PencilKit drawing bytes are retained without activating a web drawing interface.
- **Account management:** Username/password owner sign-in, salted PBKDF2-SHA256 verification, revocable sessions and credential changes in Settings.

## Quick start

You need Docker Engine with the Compose plugin, and HTTPS via a reverse proxy that you control.

```bash
git clone https://github.com/muhdusama/Agentic-Compendium.git
cd Agentic-Compendium
cp .env.example .env
chmod 600 .env
```

Edit `.env` to replace all three example credential values with independently generated secrets (`openssl rand -hex 32`). Set `APP_PUBLIC_ORIGIN` to the **exact HTTPS origin** used in your browser, with no trailing slash. Configure `APP_HERMES_ALLOWED_ORIGINS` to your actual HTTPS Hermes dashboard origin, or leave it empty to disable new Hermes connections. Set `APP_BIND_IP` to `127.0.0.1` when the HTTPS proxy runs on the same host, or to your private LAN address when the proxy runs on another trusted machine.

```bash
docker compose --env-file .env up -d --build
```

Visit **`https://your-host.example/app/`**, then sign in as **`owner`** with the `APP_OWNER_PASSWORD` from your private `.env`. You can change the username and password in Settings. The bootstrap password is stored as a salted verifier in the SQLite database on first run, and changing it does not reset it on subsequent container restarts.

## Persistent host storage

```text
Agentic-Compendium/
├── Dockerfile
├── compose.yaml
├── .env                   # secret, untracked
├── server/                # public application source
└── data/                  # private, untracked host directory
    └── deliveries.sqlite3
```

Compose mounts **`./data:/data`**. No Docker-managed volume, second application container or external database is required. Back up the SQLite database using a *consistent SQLite backup*, especially while the application is running. Do not commit or publish `data/`, `.env`, downloaded exports, user notes or API tokens.

## Important security boundaries

- This is a **single-owner** application. Keep it on LAN/VPN HTTPS and do not expose the backend port or SQLite directory to the public Internet.
- The owner web session uses HttpOnly, SameSite cookies, origin checks and server-side session revocation. Existing delivery-read and delivery-publish bearer tokens are separate credentials.
- Hermes dashboard credentials are held in the private server database, not shipped in the web application. Only allowlisted HTTPS Hermes origins are accepted.
- Offline browser storage is device-local and may contain sensitive delivery content. Avoid using shared devices; signing out is not a guarantee of device-storage erasure.
- Native PencilKit data and historical web drawings are retained on import but are not editable in the PWA.
- Hermes currently only exposes a bounded recent-run history for some deployments. This client does not promise gap-free catch-up after long outages.

## API and development

The legacy delivery APIs remain supported: `GET /healthz`, authenticated `GET /v1/status`, `GET /v1/deliveries` and idempotent `POST /v1/deliveries`. The private PWA uses `/app/api/*` for owner login, revisions, channel/section state, note synchronisation, native imports and Hermes reading.

No Node.js dependency is required at runtime. The browser UI uses standard JavaScript, CSS, IndexedDB and a service worker. Run the Python regression tests with:

```bash
python3 -m unittest discover -s server/tests -q
```

The optional browser smoke test requires a separately installed Playwright package and a disposable local test database; it must **never** target production data.

## Contributing and support

Issues and pull requests are welcome. Never include real credentials, private domains, internal IP addresses, personal deliveries or production database exports in reports, screenshots or commits.

**Developed and maintained by [IMUVERSE](https://imuverse.com).**

If the project helps you, [buy me a coffee](https://buymeacoffee.com/muhdusama). The same link is available in the application's lower-left footer and through GitHub's support button.

Licensed under the [MIT License](LICENSE).
