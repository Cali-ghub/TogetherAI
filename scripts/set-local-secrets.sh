#!/usr/bin/env bash
# Create the local TogetherAI runtime environment with masked password prompts.
set -euo pipefail
umask 077

APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${TOGETHER_ENV_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/togetherai/env}"
RUNTIME_DIR="${TOGETHER_RUNTIME_DIR:-$APP_ROOT/.runtime}"
HERMES_HOME="${TOGETHER_HERMES_HOME:-$APP_ROOT/.engine-home}"
mkdir -p "$(dirname "$TARGET")"
chmod 700 "$(dirname "$TARGET")"

read_required() {
  local prompt="$1" result_name="$2" value
  while true; do
    read -r -p "$prompt: " value
    if [[ -n "$value" ]]; then
      printf -v "$result_name" '%s' "$value"
      return
    fi
    printf '%s\n' 'A value is required.' >&2
  done
}
read_password() {
  local prompt="$1" result_name="$2" confirm value
  while true; do
    read -r -s -p "$prompt (12–512 characters): " value
    printf '\n'
    if (( ${#value} < 12 || ${#value} > 512 )); then
      printf '%s\n' 'Password must be 12–512 characters.' >&2
      continue
    fi
    read -r -s -p 'Confirm password: ' confirm
    printf '\n'
    if [[ "$value" == "$confirm" ]]; then
      printf -v "$result_name" '%s' "$value"
      return
    fi
    printf '%s\n' 'Passwords did not match; try again.' >&2
  done
}

read_required 'First participant username' USER_1_USERNAME
read_required 'First participant display name' USER_1_DISPLAY_NAME
read_password 'First participant password' USER_1_PASSWORD
read_required 'Second participant username' USER_2_USERNAME
read_required 'Second participant display name' USER_2_DISPLAY_NAME
read_password 'Second participant password' USER_2_PASSWORD

if [[ "${USER_1_USERNAME,,}" == "${USER_2_USERNAME,,}" ]]; then
  printf '%s\n' 'Participant usernames must be different; no changes were made.' >&2
  exit 1
fi
for username in "$USER_1_USERNAME" "$USER_2_USERNAME"; do
  if [[ ! "$username" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
    printf '%s\n' 'Usernames must start with a letter/number and contain only letters, numbers, dot, underscore, or hyphen.' >&2
    exit 1
  fi
done

ROOM_TITLE="${TOGETHER_DEFAULT_ROOM_TITLE:-Shared conversation}"
DB_PATH="${TOGETHER_DB_PATH:-$RUNTIME_DIR/together.db}"
UPLOAD_DIR="${TOGETHER_UPLOAD_DIR:-$RUNTIME_DIR/uploads}"
HERMES_PROFILE="${TOGETHER_HERMES_PROFILE:-together}"
HERMES_MODEL="${TOGETHER_HERMES_MODEL:-gpt-6-luna}"
HERMES_PROVIDER="${TOGETHER_HERMES_PROVIDER:-openai-codex}"
HERMES_TOOLSETS="${TOGETHER_HERMES_TOOLSETS:-web,vision,image_gen,memory}"
if [[ ! "$HERMES_PROFILE" =~ ^[A-Za-z0-9_-]{1,64}$ || "${HERMES_PROFILE,,}" == 'default' ]]; then
  printf '%s\n' 'Hermes profile must be a dedicated non-default name using letters, numbers, underscore, or hyphen.' >&2
  exit 1
fi
if [[ -z "$HERMES_MODEL" || -z "$HERMES_PROVIDER" || -z "$DB_PATH" || -z "$UPLOAD_DIR" || -z "$ROOM_TITLE" || ${#ROOM_TITLE} -gt 120 ]]; then
  printf '%s\n' 'Model, provider, database path, upload path, and a 1–120 character room title are required.' >&2
  exit 1
fi
if [[ ! "$HERMES_TOOLSETS" =~ ^[A-Za-z0-9_-]+(,[A-Za-z0-9_-]+)*$ ]]; then
  printf '%s\n' 'Toolsets must be comma-separated Hermes toolset names.' >&2
  exit 1
fi

read -r -p 'Use secure cookies? [Y/n] ' COOKIE_ANSWER
COOKIE_SECURE=true
if [[ "$COOKIE_ANSWER" =~ ^[Nn]$ ]]; then COOKIE_SECURE=false; fi

if [[ -e "$TARGET" ]]; then
  BACKUP="${TARGET}.before-$(date -u +%Y%m%dT%H%M%SZ)"
  cp -p "$TARGET" "$BACKUP"
  printf 'Existing environment backed up to %s\n' "$BACKUP"
fi
TMP="$(mktemp "${TARGET}.tmp.XXXXXXXX")"
trap 'rm -f "$TMP"' EXIT
SESSION_SECRET="$(openssl rand -hex 48)"
HERMES_EXECUTABLE="${TOGETHER_HERMES_EXECUTABLE:-$(command -v hermes || printf hermes)}"
{
  printf 'TOGETHER_SESSION_SECRET=%q\n' "$SESSION_SECRET"
  printf 'TOGETHER_USER_1_USERNAME=%q\n' "$USER_1_USERNAME"
  printf 'TOGETHER_USER_1_DISPLAY_NAME=%q\n' "$USER_1_DISPLAY_NAME"
  printf 'TOGETHER_USER_1_PASSWORD=%q\n' "$USER_1_PASSWORD"
  printf 'TOGETHER_USER_2_USERNAME=%q\n' "$USER_2_USERNAME"
  printf 'TOGETHER_USER_2_DISPLAY_NAME=%q\n' "$USER_2_DISPLAY_NAME"
  printf 'TOGETHER_USER_2_PASSWORD=%q\n' "$USER_2_PASSWORD"
  printf 'TOGETHER_COOKIE_SECURE=%q\n' "$COOKIE_SECURE"
  printf 'TOGETHER_DEFAULT_ROOM_TITLE=%q\n' "$ROOM_TITLE"
  printf 'TOGETHER_RUNTIME_DIR=%q\n' "$RUNTIME_DIR"
  printf 'TOGETHER_DB_PATH=%q\n' "$DB_PATH"
  printf 'TOGETHER_UPLOAD_DIR=%q\n' "$UPLOAD_DIR"
  printf 'TOGETHER_HERMES_HOME=%q\n' "$HERMES_HOME"
  printf 'TOGETHER_HERMES_EXECUTABLE=%q\n' "$HERMES_EXECUTABLE"
  printf 'TOGETHER_HERMES_PROFILE=%q\n' "$HERMES_PROFILE"
  printf 'TOGETHER_HERMES_MODEL=%q\n' "$HERMES_MODEL"
  printf 'TOGETHER_HERMES_PROVIDER=%q\n' "$HERMES_PROVIDER"
  printf 'TOGETHER_HERMES_TOOLSETS=%q\n' "$HERMES_TOOLSETS"
} >"$TMP"
chmod 600 "$TMP"
mv -f "$TMP" "$TARGET"
trap - EXIT
printf 'Environment saved securely at %s (mode 0600). Secret values were not printed.\n' "$TARGET"
printf 'Set up the isolated Hermes profile and authentication before starting the app; see README.md.\n'
