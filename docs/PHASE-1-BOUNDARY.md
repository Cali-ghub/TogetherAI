# TogetherAI security and memory boundary

```text
Browser -- session + CSRF --> TogetherAI app -- minimal environment --> dedicated Hermes home
                                     |                                      |
                                     |                                      +-- configured model/provider/toolsets
                                     +-- SQLite + private upload directory
```

## Application controls

- Login credentials are configured as two local participant slots and stored as password hashes in the local SQLite database.
- Room access and attachment access are checked against authenticated membership. Browser-facing room and attachment identifiers are opaque.
- The app passes only the submitted prompt and authorized attachment path to the Hermes adapter. The room ID is not included in the Hermes command.
- The Hermes subprocess receives a minimal environment rooted at the configured profile home and its stderr is not returned to the browser.
- The service template binds to loopback. Put HTTPS and any network exposure controls in a separately reviewed reverse proxy/firewall configuration.

## Hermes setup and capabilities

The dedicated profile home is expected at `${TOGETHER_HERMES_HOME}/profiles/${TOGETHER_HERMES_PROFILE}`. The adapter rejects the default profile, rejects a profile symlink, and validates that the profile directory and executable exist at application startup. The model, provider and comma-separated toolsets are explicit configuration values; defaults are `gpt-6-luna`, `openai-codex`, and `web,vision,image_gen,memory`.

The process is isolated from the app's inherited environment, but this is not a complete operating-system sandbox. Enabled Hermes tools may access resources available to the service account and may send user content to the configured provider. Review/disable unnecessary toolsets and use a dedicated OS account with narrow filesystem permissions.

## Important private-room limitation

Room membership protects application/database retrieval. It does **not** partition Hermes persistent memory. Every room currently invokes the same Hermes profile, and profile-level memory may be recalled or updated across rooms. The app does not yet enforce “no memory reads or writes in private rooms” per request. Do not treat private rooms as a safe place for sensitive content unless persistent memory is disabled at the Hermes profile level and that behavior is independently verified, or a true per-room memory boundary is implemented.

## Deliberately not included

- No credentials, live profiles, chat database, uploads, certificates, local paths or production service configuration.
- No external listener or automatic service installation.
- No claim that prompt text alone enforces a memory boundary or operating-system sandbox.
- No GitHub remote, commit, push, repository visibility, or license selection.
