# TogetherAI

TogetherAI is a small, self-hosted shared chat for **two configured participants**, backed by a dedicated Hermes Agent profile. It is a template for households or small trusted groups—not a multi-tenant service.

The repository contains the application and safe setup templates only. It intentionally excludes credentials, Hermes profile state, databases, uploaded images, logs, certificates, and deployment-specific paths.

## Features

- Two locally configured accounts with password hashing and session/CSRF protection.
- Shared and owner-only rooms, searchable chat history, image attachments and gallery, and prompt retry/edit.
- A Hermes CLI adapter that invokes one dedicated profile with a configured model, provider and toolset list.
- A deterministic fake engine and `unittest` suite for development without an LLM call.

## Requirements

- Python 3.11 or newer.
- A compatible Hermes Agent CLI installation (`hermes` on `PATH` or an explicit executable path).
- Hermes authentication configured inside the dedicated profile. The default model/provider are `gpt-6-luna` and `openai-codex`; override them if your Hermes installation/provider supports different values.
- A model/provider account with any required subscription or usage allowance. The app does not supply model credentials.

## First-time local setup

1. Install the package and test dependency:

   ```bash
   python3 -m pip install -e '.[test]'
   ```

2. Create a local environment file. The script asks for both participants' usernames, display names and passwords using masked password prompts. It writes the file outside the repository with owner-only permissions:

   ```bash
   chmod +x scripts/set-local-secrets.sh
   scripts/set-local-secrets.sh
   ```

   The default file is `~/.config/togetherai/env`; override its location with `TOGETHER_ENV_FILE` when running the script. To customize storage paths or Hermes profile/model/provider/toolsets during setup, export the corresponding `TOGETHER_*` variable when invoking the script; it writes those values into the generated file. You can also edit that file locally afterward. It creates a backup before replacing an existing file. Do not commit this file or paste passwords into chat/issues.

3. Create and authenticate the dedicated Hermes home. This is separate from your normal Hermes profile and must not be a symlink to it:

   ```bash
   set -a
   . "$HOME/.config/togetherai/env"
   set +a
   PROFILE_HOME="$TOGETHER_HERMES_HOME/profiles/$TOGETHER_HERMES_PROFILE"
   mkdir -p "$PROFILE_HOME"
   HERMES_HOME="$PROFILE_HOME" HERMES_BASE_HOME="$TOGETHER_HERMES_HOME" \
     hermes auth add "$TOGETHER_HERMES_PROVIDER" --type oauth
   ```

   Complete provider sign-in locally. The default `openai-codex` uses OAuth; for an API-key provider, use `--type api-key` instead and enter its key only at Hermes' local prompt. Follow the provider's Hermes setup instructions if it requires a different flow. Set `TOGETHER_HERMES_PROVIDER` and `TOGETHER_HERMES_MODEL` in the local environment file to match. Keep the profile directory private; it contains authentication and possibly memory/session data. The profile environment variables ensure these commands do not configure your default Hermes home.

4. Start the development server. For plain loopback HTTP, rerun the setup script and answer `n` to secure-cookie mode. Secure cookies should remain enabled whenever HTTPS is used:

   ```bash
   set -a
   . "$HOME/.config/togetherai/env"
   set +a
   python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8790
   ```

5. Open <http://127.0.0.1:8790>. On first launch the app bootstraps two participants and creates the configured initial room.

The model adapter launches Hermes only when a prompt is submitted. It does not make an LLM call during installation or tests.

## Configuration reference

The reference-only variable template is `.env.example`. The app does not load it and it is not a shell source file. It contains no usable credentials; do not fill it with real secrets and commit it. The setup script writes the configured values to a separate local file.

| Variable | Default | Purpose |
|---|---|---|
| `TOGETHER_SESSION_SECRET` | Required | Random secret for signing session cookies; setup script generates one. |
| `TOGETHER_USER_1_USERNAME` / `TOGETHER_USER_2_USERNAME` | `member1` / `member2` | Distinct login identifiers. |
| `TOGETHER_USER_1_DISPLAY_NAME` / `TOGETHER_USER_2_DISPLAY_NAME` | `Participant 1` / `Participant 2` | Names shown in the UI and chat. |
| `TOGETHER_USER_1_PASSWORD` / `TOGETHER_USER_2_PASSWORD` | Required | 12–512 characters; never store in source control. |
| `TOGETHER_COOKIE_SECURE` | `true` | Use `true` for HTTPS; set `false` only for loopback HTTP development. |
| `TOGETHER_DEFAULT_ROOM_TITLE` | `Shared conversation` | Title of the initial shared room. |
| `TOGETHER_DB_PATH` | `.runtime/together.db` | SQLite database path. |
| `TOGETHER_UPLOAD_DIR` | `.runtime/uploads` | Private image storage directory. |
| `TOGETHER_HERMES_HOME` | `.engine-home` | Root for the app's isolated Hermes profile. |
| `TOGETHER_HERMES_EXECUTABLE` | `hermes` from `PATH` | Hermes CLI executable; can be an absolute path. |
| `TOGETHER_HERMES_PROFILE` | `together` | Dedicated non-default profile directory name under `profiles/`. |
| `TOGETHER_HERMES_MODEL` | `gpt-6-luna` | Model name passed to Hermes. |
| `TOGETHER_HERMES_PROVIDER` | `openai-codex` | Provider passed to Hermes. |
| `TOGETHER_HERMES_TOOLSETS` | `web,vision,image_gen,memory` | Comma-separated Hermes toolsets enabled for the app. Review these capabilities before deployment. |

The two account slots are intentional. This build does not support arbitrary account creation or groups.

## Run checks

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q app tests
node --check static/app.js
```

Tests use temporary databases, uploads and fake Hermes paths. They do not require model credentials or call an LLM.

## Deployment

`deploy/hermes-together.service` is a **template**, not a ready-to-install unit. Replace its example user, repository and state paths; install dependencies in a dedicated environment; set up the Hermes profile and environment file; then review systemd write paths. Its `ExecStart` uses `scripts/run-service.sh` to source the shell-escaped environment generated by the setup script; do not replace this with systemd's `EnvironmentFile=` unless you convert the file to systemd's distinct syntax. Keep the web process bound to loopback behind a TLS-terminating reverse proxy. Do not expose Uvicorn directly to the public internet. Restrict the proxy, host firewall, file permissions and backups according to your deployment's threat model.

The example unit sets `ProtectHome=true`. Install Hermes and its Python environment somewhere the service can execute outside `/home` (for example, a dedicated `/opt` virtual environment) and set `TOGETHER_HERMES_EXECUTABLE` to that executable when creating the environment file. If your installation must live under a home directory, review the systemd isolation settings deliberately rather than silently weakening them.

For a deployment using the template's `/opt/togetherai` and `/var/lib/togetherai` paths, run the setup script as the intended service account after granting it access to the config directory:

```bash
TOGETHER_ENV_FILE=/etc/togetherai/env \
TOGETHER_RUNTIME_DIR=/var/lib/togetherai \
TOGETHER_HERMES_HOME=/var/lib/togetherai/hermes \
TOGETHER_HERMES_EXECUTABLE=/opt/hermes-venv/bin/hermes \
scripts/set-local-secrets.sh

set -a
. /etc/togetherai/env
set +a
PROFILE_HOME="$TOGETHER_HERMES_HOME/profiles/$TOGETHER_HERMES_PROFILE"
mkdir -p "$PROFILE_HOME"
HERMES_HOME="$PROFILE_HOME" HERMES_BASE_HOME="$TOGETHER_HERMES_HOME" \
  hermes auth add "$TOGETHER_HERMES_PROVIDER" --type oauth
```

For an API-key provider, replace `--type oauth` with `--type api-key`; Hermes will prompt for the key locally. Ensure the service account owns the environment file with mode `0600` and can write only to `/var/lib/togetherai`. Adjust the Hermes executable/Python paths in the unit for your installation. The setup commands are examples; review them for your host before running. See the [Hermes profiles guide](https://hermes-agent.nousresearch.com/docs/user-guide/profiles) and [provider setup guide](https://hermes-agent.nousresearch.com/docs/integrations/providers) for provider-specific requirements.

No deployment command is run by the application or setup script. The template does not modify an existing Hermes service.

## Privacy and memory boundary

Application authorization limits which rooms and uploaded files each signed-in participant can read. **That is not the same as a private persistent-memory boundary.** All rooms currently use one Hermes profile. Hermes profile-level memory may be available across rooms, including owner-only rooms; the app does not yet enforce “no memory read/write in private rooms” at the execution boundary. Avoid putting sensitive information in private rooms until that limitation is fixed or persistent memory is disabled and independently verified for the profile. The configured Hermes tools also send prompts and selected images to the chosen provider; review the provider's data handling before use.

## GitHub and licensing

Before publishing, review Git's exact candidate file list and rerun the tests/privacy scan. Choose a license that matches the reuse rights you intend to grant; public visibility alone does not grant permission to reuse the code. The project does not create a GitHub repository or configure a remote automatically.
