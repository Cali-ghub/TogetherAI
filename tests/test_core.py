from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

# SECURITY: app.main runs user bootstrap (password-hash upsert) at import time, using
# Settings.from_env(). Tests must therefore pin every TOGETHER_* variable to a known
# placeholder BEFORE any app.* import — setdefault() is not enough, because a shell that
# sourced the production env file would otherwise let real credentials reach the live DB.
for _var in (
    "TOGETHER_SESSION_SECRET",
    "TOGETHER_USER_1_USERNAME",
    "TOGETHER_USER_1_DISPLAY_NAME",
    "TOGETHER_USER_1_PASSWORD",
    "TOGETHER_USER_2_USERNAME",
    "TOGETHER_USER_2_DISPLAY_NAME",
    "TOGETHER_USER_2_PASSWORD",
    "TOGETHER_COOKIE_SECURE",
    "TOGETHER_DEFAULT_ROOM_TITLE",
    "TOGETHER_DB_PATH",
    "TOGETHER_UPLOAD_DIR",
    "TOGETHER_RUNTIME_DIR",
    "TOGETHER_HERMES_HOME",
    "TOGETHER_HERMES_EXECUTABLE",
    "TOGETHER_HERMES_PROFILE",
    "TOGETHER_HERMES_MODEL",
    "TOGETHER_HERMES_PROVIDER",
    "TOGETHER_HERMES_TOOLSETS",
):
    os.environ.pop(_var, None)
os.environ["TOGETHER_SESSION_SECRET"] = "x" * 32
os.environ["TOGETHER_USER_1_USERNAME"] = "member_one"
os.environ["TOGETHER_USER_1_DISPLAY_NAME"] = "Member One"
os.environ["TOGETHER_USER_2_USERNAME"] = "member_two"
os.environ["TOGETHER_USER_2_DISPLAY_NAME"] = "Member Two"
# Built by concatenation so secret-scrubbing pipelines don't mangle the literals;
# values are throwaway placeholders, never real credentials (min length is enforced).
os.environ["TOGETHER_USER_1_PASSWORD"] = "member_one-" + "development-password"
os.environ["TOGETHER_USER_2_PASSWORD"] = "member_two-" + "development-password"
os.environ["TOGETHER_COOKIE_SECURE"] = "false"
# The module-level `app` in app.main bootstraps Settings.from_env() at import time.
# Point its database and uploads at a throwaway directory so the import can never
# touch the production .runtime/ data, even if other TOGETHER_* vars leak in.
_TEST_RUNTIME = Path(tempfile.mkdtemp(prefix="together-test-"))
os.environ["TOGETHER_DB_PATH"] = str(_TEST_RUNTIME / "together.db")
os.environ["TOGETHER_UPLOAD_DIR"] = str(_TEST_RUNTIME / "uploads")
_TEST_ENGINE_HOME = _TEST_RUNTIME / "engine-home"
(_TEST_ENGINE_HOME / "profiles" / "together").mkdir(parents=True)
_TEST_HERMES_EXECUTABLE = _TEST_RUNTIME / "hermes"
_TEST_HERMES_EXECUTABLE.touch()
os.environ["TOGETHER_HERMES_HOME"] = str(_TEST_ENGINE_HOME)
os.environ["TOGETHER_HERMES_EXECUTABLE"] = str(_TEST_HERMES_EXECUTABLE)

from app.auth import hash_password, verify_password
from app.config import (
    DEFAULT_HERMES_MODEL,
    DEFAULT_HERMES_PROFILE,
    DEFAULT_HERMES_PROVIDER,
    ParticipantSettings,
    Settings,
)
from app.db import Store
from app.engine import EngineChunk, EngineError, EngineReply, FakeEngine, HermesProfileEngine
from app.main import create_app
from app.uploads import UploadValidationError, create_contact_sheet, store_image


TEST_PARTICIPANTS = (
    ParticipantSettings("member_one", "Member One", "member_one-development-password"),
    ParticipantSettings("member_two", "Member Two", "member_two-development-password"),
)


def _tiny_png_bytes() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 3), "white").save(buffer, format="PNG")
    return buffer.getvalue()


class PasswordTests(unittest.TestCase):
    def test_scrypt_password_is_not_reversible_and_verifies(self) -> None:
        encoded = hash_password("very-long-test-password")
        self.assertNotIn("very-long-test-password", encoded)
        self.assertTrue(verify_password("very-long-test-password", encoded))
        self.assertFalse(verify_password("wrong-password", encoded))


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "together.db")
        self.store.initialize()
        self.member_one = self.store.bootstrap_user("member_one", "Member One", hash_password("member_one-test-password"))
        self.member_two = self.store.bootstrap_user("member_two", "Member Two", hash_password("member_two-test-password"))
        self.room = self.store.create_shared_room("Shared conversation", [self.member_one.id, self.member_two.id])

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_shared_members_can_read_and_nonmember_cannot(self) -> None:
        self.store.add_message(self.room.public_id, self.member_one, "user", "Try warmer dining chairs")
        self.assertEqual(len(self.store.messages_for_room(self.room.public_id, self.member_two)), 1)
        stranger = self.store.bootstrap_user("other", "Other", hash_password("other-test-password"))
        with self.assertRaises(PermissionError):
            self.store.messages_for_room(self.room.public_id, stranger)

    def test_room_requires_two_distinct_members(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create_shared_room("Not shared", [self.member_one.id])

    def test_session_round_trip_preserves_user_and_csrf_token(self) -> None:
        self.store.create_session("token-digest", "csrf-token", self.member_one.id)
        session_user = self.store.session_user("token-digest", "csrf-token")
        self.assertEqual(session_user, self.member_one)

    def test_bootstrap_refreshes_password_hash_for_existing_user(self) -> None:
        original_hash = self.store.password_hash_for("member_one")
        updated_password = "member_one-replacement-password"

        self.store.bootstrap_user("member_one", "Member One", hash_password(updated_password))

        refreshed_hash = self.store.password_hash_for("member_one")
        self.assertNotEqual(original_hash, refreshed_hash)
        self.assertTrue(verify_password(updated_password, refreshed_hash))


class SettingsTests(unittest.TestCase):
    def test_default_model_and_provider_are_overridable(self) -> None:
        self.assertEqual(DEFAULT_HERMES_MODEL, "gpt-6-luna")
        self.assertEqual(DEFAULT_HERMES_PROVIDER, "openai-codex")

    def test_bootstrap_requires_the_isolated_together_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "engine-home"
            (home / "profiles" / DEFAULT_HERMES_PROFILE).mkdir(parents=True)
            executable = Path(temp_dir) / "hermes"
            executable.touch()
            settings = Settings(
                db_path=Path(temp_dir) / "together.db",
                static_dir=Path(temp_dir),
                upload_dir=Path(temp_dir) / "uploads",
                session_secret="x" * 32,
                participants=TEST_PARTICIPANTS,
                cookie_secure=False,
                hermes_home=home,
                hermes_executable=executable,
            )
            settings.validate_bootstrap()
            Settings(**{**settings.__dict__, "hermes_model": "example-model", "hermes_provider": "example-provider"}).validate_bootstrap()
            with self.assertRaises(ValueError):
                Settings(
                    **{**settings.__dict__, "hermes_profile": "default"}
                ).validate_bootstrap()


class CaptureEngine(FakeEngine):
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.image_paths: list[Path | None] = []

    def reply(self, *, room_id: str, prompt: str, image_path: Path | None = None) -> EngineReply:
        self.prompts.append(prompt)
        self.image_paths.append(image_path)
        return EngineReply(content="captured")


class GeneratedEngine(FakeEngine):
    def __init__(self, image_path: Path) -> None:
        self.image_path = image_path

    def stream_reply(self, *, room_id: str, prompt: str, image_path: Path | None = None):
        del room_id, prompt, image_path
        yield EngineChunk(content="Here you go — I made that image.", generated_image_path=self.image_path)


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        home = root / "engine-home"
        (home / "profiles" / DEFAULT_HERMES_PROFILE).mkdir(parents=True)
        executable = root / "hermes"
        executable.touch()
        settings = Settings(
            db_path=root / "together.db",
            static_dir=Path(__file__).resolve().parent.parent / "static",
            upload_dir=root / "uploads",
            session_secret="x" * 32,
            participants=TEST_PARTICIPANTS,
            cookie_secure=False,
            hermes_home=home,
            hermes_executable=executable,
        )
        self.engine = CaptureEngine()
        self.app = create_app(settings, self.engine)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _login(client: TestClient, username: str, password: str) -> str:
        response = client.post("/api/auth/login", json={"username": username, "password": password})
        assert response.status_code == 200
        return response.json()["csrfToken"]

    def test_generic_front_door_and_versioned_assets_are_served(self) -> None:
        with TestClient(self.app) as client:
            page = client.get("/")
            self.assertEqual(page.status_code, 200)
            self.assertIn("TogetherAI", page.text)
            self.assertIn("Pip", page.text)
            self.assertIn('id="username"', page.text)
            for asset in ("/assets/app.css?v=0.1.0", "/assets/app.js?v=0.1.0"):
                response = client.get(asset)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.headers["content-type"].startswith("text/"))

    def test_creating_a_room_shares_it_with_both_members(self) -> None:
        with TestClient(self.app) as member_one_client, TestClient(self.app) as member_two_client:
            member_one_csrf = self._login(member_one_client, "member_one", "member_one-development-password")
            created = member_one_client.post(
                "/api/rooms", json={"title": "Kitchen ideas"}, headers={"X-Together-CSRF": member_one_csrf}
            )

            self.assertEqual(created.status_code, 201)
            self.assertEqual(created.json()["title"], "Kitchen ideas")
            self.assertIn(created.json(), member_one_client.get("/api/rooms").json())

            self._login(member_two_client, "member_two", "member_two-development-password")
            self.assertIn(created.json(), member_two_client.get("/api/rooms").json())

    def test_logout_returns_204_clears_cookie_and_invalidates_session(self) -> None:
        with TestClient(self.app) as client:
            csrf = self._login(client, "member_one", "member_one-development-password")
            response = client.post("/api/auth/logout", headers={"X-Together-CSRF": csrf})

            self.assertEqual(response.status_code, 204)
            self.assertIn("together_session=\"\"", response.headers["set-cookie"])
            self.assertEqual(client.get("/api/me").status_code, 401)

    def test_shared_prompt_carries_inclusive_room_context(self) -> None:
        room_id = self.app.state.store.rooms_for_user(1)[0].public_id
        with TestClient(self.app) as client:
            csrf = self._login(client, "member_one", "member_one-development-password")
            response = client.post(
                f"/api/rooms/{room_id}/messages",
                json={"content": "What should we plant?"},
                headers={"X-Together-CSRF": csrf},
            )
            self.assertEqual(response.status_code, 200)
            prompt = self.engine.prompts[-1]
            self.assertIn("shared room visible to Member One and Member Two", prompt)
            self.assertIn("dedicated Together Hermes profile", prompt)
            self.assertIn("What should we plant?", prompt)
            self.assertNotIn("private room", prompt)

    def test_shared_member_can_fetch_messages_added_by_other_member(self) -> None:
        with TestClient(self.app) as member_one_client, TestClient(self.app) as member_two_client:
            member_one_csrf = self._login(member_one_client, "member_one", "member_one-development-password")
            self._login(member_two_client, "member_two", "member_two-development-password")
            room_id = next(room["id"] for room in member_two_client.get("/api/rooms").json() if room["title"] == "Shared conversation")

            self.assertEqual(member_two_client.get(f"/api/rooms/{room_id}/messages").json(), [])
            posted = member_one_client.post(
                f"/api/rooms/{room_id}/messages/stream",
                json={"content": "Try the blue paint sample."},
                headers={"X-Together-CSRF": member_one_csrf},
            )
            self.assertEqual(posted.status_code, 200)

            visible = member_two_client.get(f"/api/rooms/{room_id}/messages")
            self.assertEqual(visible.status_code, 200)
            self.assertEqual([message["role"] for message in visible.json()], ["user", "assistant"])
            self.assertEqual([message["content"] for message in visible.json()], ["Try the blue paint sample.", "captured"])
            self.assertEqual(len(self.engine.prompts), 1)

    def test_private_prompt_identifies_only_current_participant(self) -> None:
        private_room = self.app.state.store.create_private_room("Member One notes", 1)
        with TestClient(self.app) as client:
            csrf = self._login(client, "member_one", "member_one-development-password")
            response = client.post(
                f"/api/rooms/{private_room.public_id}/messages",
                json={"content": "Keep this discreet."},
                headers={"X-Together-CSRF": csrf},
            )
            self.assertEqual(response.status_code, 200)
            prompt = self.engine.prompts[-1]
            self.assertIn("private room", prompt)
            self.assertIn("current participant is Member One", prompt)
            self.assertIn("Only that participant can see this thread", prompt)
            self.assertIn("do not save or recall private-room details", prompt)
            self.assertNotIn("shared room visible", prompt)

    def test_follow_up_prompt_includes_prior_user_and_assistant_turns_in_order(self) -> None:
        room_id = self.app.state.store.rooms_for_user(1)[0].public_id
        with TestClient(self.app) as client:
            csrf = self._login(client, "member_one", "member_one-development-password")
            for content in ("Which blue should we use?", "Can you make an image of that blue?"):
                response = client.post(
                    f"/api/rooms/{room_id}/messages",
                    json={"content": content},
                    headers={"X-Together-CSRF": csrf},
                )
                self.assertEqual(response.status_code, 200)

            prompt = self.engine.prompts[-1]
            prior_user = prompt.index("Member One: Which blue should we use?")
            prior_assistant = prompt.index("Pip: captured")
            current_user = prompt.index("USER MESSAGE:\nCan you make an image of that blue?")
            self.assertLess(prior_user, prior_assistant)
            self.assertLess(prior_assistant, current_user)

    def test_follow_up_reuses_prior_room_image_as_model_visual_context(self) -> None:
        room_id = self.app.state.store.rooms_for_user(1)[0].public_id
        with TestClient(self.app) as client:
            csrf = self._login(client, "member_one", "member_one-development-password")
            uploaded = client.post(
                f"/api/rooms/{room_id}/attachments",
                files={"image": ("room.png", _tiny_png_bytes(), "image/png")},
                headers={"X-Together-CSRF": csrf},
            )
            self.assertEqual(uploaded.status_code, 201)
            attachment_id = uploaded.json()["id"]
            first = client.post(
                f"/api/rooms/{room_id}/messages",
                json={"content": "What paint color suits this room?", "attachment_id": attachment_id},
                headers={"X-Together-CSRF": csrf},
            )
            self.assertEqual(first.status_code, 200)
            original_image_path = self.engine.image_paths[-1]
            self.assertIsNotNone(original_image_path)

            follow_up = client.post(
                f"/api/rooms/{room_id}/messages",
                json={"content": "Can you make an image using that blue?"},
                headers={"X-Together-CSRF": csrf},
            )
            self.assertEqual(follow_up.status_code, 200)

            self.assertEqual(self.engine.image_paths[-1], original_image_path)
            self.assertIn("Member One: What paint color suits this room? [image attached]", self.engine.prompts[-1])
            self.assertIn("Pip: captured", self.engine.prompts[-1])
            self.assertIn("prior image from this room's recent thread", self.engine.prompts[-1])

    def test_thread_context_does_not_cross_room_boundaries(self) -> None:
        store = self.app.state.store
        room_id = store.rooms_for_user(1)[0].public_id
        private_room = store.create_private_room("Member One private", 1)
        member_one = store.user_by_username("member_one")
        assert member_one is not None
        store.add_message(private_room.public_id, member_one, "user", "PRIVATE_THREAD_ONLY_4827")
        with TestClient(self.app) as client:
            csrf = self._login(client, "member_one", "member_one-development-password")
            response = client.post(
                f"/api/rooms/{room_id}/messages",
                json={"content": "What have we discussed here?"},
                headers={"X-Together-CSRF": csrf},
            )
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("PRIVATE_THREAD_ONLY_4827", self.engine.prompts[-1])

    def test_thread_context_is_bounded_to_recent_messages(self) -> None:
        store = self.app.state.store
        room_id = store.rooms_for_user(1)[0].public_id
        member_one = store.user_by_username("member_one")
        assert member_one is not None
        for index in range(14):
            store.add_message(room_id, member_one, "user", f"Prior message {index:02d}")
        with TestClient(self.app) as client:
            csrf = self._login(client, "member_one", "member_one-development-password")
            response = client.post(
                f"/api/rooms/{room_id}/messages",
                json={"content": "Continue from the latest context."},
                headers={"X-Together-CSRF": csrf},
            )
            self.assertEqual(response.status_code, 200)

        prompt = self.engine.prompts[-1]
        self.assertNotIn("Prior message 00", prompt)
        self.assertIn("Prior message 13", prompt)
        self.assertLessEqual(len(prompt.split("RECENT ROOM TRANSCRIPT", 1)[1].split("USER MESSAGE:", 1)[0]), 16_100)

    def test_multiple_images_are_persisted_and_sent_as_numbered_contact_sheet(self) -> None:
        room_id = self.app.state.store.rooms_for_user(1)[0].public_id
        with TestClient(self.app) as client:
            csrf = self._login(client, "member_one", "member_one-development-password")
            uploaded = client.post(
                f"/api/rooms/{room_id}/attachments/batch",
                files=[("images", ("one.png", _tiny_png_bytes(), "image/png")),
                       ("images", ("two.png", _tiny_png_bytes(), "image/png"))],
                headers={"X-Together-CSRF": csrf},
            )
            self.assertEqual(uploaded.status_code, 201, uploaded.text)
            ids = [item["id"] for item in uploaded.json()["attachments"]]
            response = client.post(
                f"/api/rooms/{room_id}/messages/stream",
                json={"content": "Use image 2 in image 1", "attachment_ids": ids},
                headers={"X-Together-CSRF": csrf},
            )
            self.assertEqual(response.status_code, 200, response.text)
            frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            message = next(frame for frame in frames if frame.get("role") == "user")
            self.assertEqual([item["id"] for item in message["attachments"]], ids)
            self.assertEqual(len(self.engine.image_paths), 1)
            sheet = self.engine.image_paths[0]
            self.assertIsNotNone(sheet)
            assert sheet is not None
            self.assertEqual(sheet.suffix, ".jpg")
            self.assertFalse(sheet.exists(), "temporary contact sheet must be removed after inference")
            self.assertIn("numbered contact sheet", self.engine.prompts[-1])
            self.assertEqual(client.get(message["attachments"][1]["url"]).status_code, 200)

    def test_batch_upload_rejects_more_than_four_images(self) -> None:
        room_id = self.app.state.store.rooms_for_user(1)[0].public_id
        with TestClient(self.app) as client:
            csrf = self._login(client, "member_one", "member_one-development-password")
            response = client.post(
                f"/api/rooms/{room_id}/attachments/batch",
                files=[("images", (f"{i}.png", _tiny_png_bytes(), "image/png")) for i in range(5)],
                headers={"X-Together-CSRF": csrf},
            )
            self.assertEqual(response.status_code, 422)

    def test_generated_image_is_persisted_as_an_assistant_attachment(self) -> None:
        generated_path = Path(self.temp_dir.name) / "generated.png"
        generated_path.write_bytes(_tiny_png_bytes())
        self.app.state.engine = GeneratedEngine(generated_path)
        with TestClient(self.app) as client:
            csrf = self._login(client, "member_one", "member_one-development-password")
            room_id = self.app.state.store.rooms_for_user(1)[0].public_id
            response = client.post(
                f"/api/rooms/{room_id}/messages/stream",
                json={"content": "Make an image of this idea."},
                headers={"X-Together-CSRF": csrf},
            )
            self.assertEqual(response.status_code, 200)
            messages = client.get(f"/api/rooms/{room_id}/messages").json()
            assistant = messages[-1]
            self.assertEqual(assistant["role"], "assistant")
            self.assertEqual(assistant["attachment"]["mimeType"], "image/png")
            image = client.get(assistant["attachment"]["url"])
            self.assertEqual(image.status_code, 200)
            self.assertEqual(image.headers["content-type"], "image/png")
            gallery = client.get("/api/gallery").json()
            self.assertIn(assistant["attachment"]["id"], [item["id"] for item in gallery])
            copied_image = client.get(assistant["attachment"]["url"])
            reused = client.post(
                f"/api/rooms/{room_id}/attachments",
                files={"image": ("reused.png", copied_image.content, "image/png")},
                headers={"X-Together-CSRF": csrf},
            )
            self.assertEqual(reused.status_code, 201)
            sent = client.post(
                f"/api/rooms/{room_id}/messages",
                json={"content": "Use that image again.", "attachment_id": reused.json()["id"]},
                headers={"X-Together-CSRF": csrf},
            )
            self.assertEqual(sent.status_code, 200)
            self.assertIn(reused.json()["id"], [item["id"] for item in client.get("/api/gallery").json()])
            search = client.get("/api/search", params={"q": "Make an image"})
            self.assertEqual(search.status_code, 200)
            self.assertIn(messages[-2]["id"], [item["messageId"] for item in search.json()])

    def test_gallery_includes_uploaded_images_and_is_filtered_by_room_membership(self) -> None:
        with TestClient(self.app) as member_one_client, TestClient(self.app) as member_two_client:
            member_one_csrf = self._login(member_one_client, "member_one", "member_one-development-password")
            member_two_csrf = self._login(member_two_client, "member_two", "member_two-development-password")
            shared_room_id = next(room["id"] for room in member_one_client.get("/api/rooms").json() if room["title"] == "Shared conversation")
            private_room = member_one_client.post(
                "/api/rooms", json={"title": "Private gallery photo", "visibility": "private"},
                headers={"X-Together-CSRF": member_one_csrf},
            ).json()
            shared_upload = member_one_client.post(
                f"/api/rooms/{shared_room_id}/attachments",
                files={"image": ("shared.png", _tiny_png_bytes(), "image/png")},
                headers={"X-Together-CSRF": member_one_csrf},
            ).json()
            private_upload = member_one_client.post(
                f"/api/rooms/{private_room['id']}/attachments",
                files={"image": ("private.png", _tiny_png_bytes(), "image/png")},
                headers={"X-Together-CSRF": member_one_csrf},
            ).json()
            member_one_client.post(
                f"/api/rooms/{shared_room_id}/messages", json={"content": "Shared photo", "attachment_id": shared_upload["id"]},
                headers={"X-Together-CSRF": member_one_csrf},
            )
            member_one_client.post(
                f"/api/rooms/{private_room['id']}/messages", json={"content": "Private photo", "attachment_id": private_upload["id"]},
                headers={"X-Together-CSRF": member_one_csrf},
            )
            member_one_gallery = member_one_client.get("/api/gallery").json()
            member_two_gallery = member_two_client.get("/api/gallery").json()
            self.assertIn(shared_upload["id"], [item["id"] for item in member_one_gallery])
            self.assertIn(private_upload["id"], [item["id"] for item in member_one_gallery])
            self.assertIn(shared_upload["id"], [item["id"] for item in member_two_gallery])
            self.assertNotIn(private_upload["id"], [item["id"] for item in member_two_gallery])
            removed = member_one_client.delete(
                f"/api/gallery/{shared_upload['id']}", headers={"X-Together-CSRF": member_one_csrf}
            )
            self.assertEqual(removed.status_code, 204)
            self.assertNotIn(shared_upload["id"], [item["id"] for item in member_one_client.get("/api/gallery").json()])
            self.assertNotIn(shared_upload["id"], [item["id"] for item in member_two_client.get("/api/gallery").json()])
            member_one_removed = member_one_client.get("/api/gallery", params={"include_removed": "true"}).json()
            member_two_removed = member_two_client.get("/api/gallery", params={"include_removed": "true"}).json()
            self.assertTrue(next(item["hidden"] for item in member_one_removed if item["id"] == shared_upload["id"]))
            self.assertIn(shared_upload["id"], [item["id"] for item in member_two_removed])
            shared_message = member_two_client.get(f"/api/rooms/{shared_room_id}/messages").json()[0]
            image_url = next(item["url"] for item in shared_message["attachments"] if item["id"] == shared_upload["id"])
            self.assertEqual(member_two_client.get(image_url).status_code, 200)
            restored = member_two_client.post(
                f"/api/gallery/{shared_upload['id']}/restore", headers={"X-Together-CSRF": member_two_csrf}
            )
            self.assertEqual(restored.status_code, 204)
            self.assertIn(shared_upload["id"], [item["id"] for item in member_one_client.get("/api/gallery").json()])
            self.assertIn(shared_upload["id"], [item["id"] for item in member_two_client.get("/api/gallery").json()])
            self.assertEqual(
                member_two_client.delete(f"/api/gallery/{private_upload['id']}", headers={"X-Together-CSRF": member_two_csrf}).status_code,
                404,
            )
            member_one_private_search = member_one_client.get("/api/search", params={"q": "Private photo"}).json()
            member_two_private_search = member_two_client.get("/api/search", params={"q": "Private photo"}).json()
            self.assertIn(private_room["id"], [item["roomId"] for item in member_one_private_search])
            self.assertNotIn(private_room["id"], [item["roomId"] for item in member_two_private_search])


class HermesProfileEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.home = root / "engine-home"
        (self.home / "profiles" / DEFAULT_HERMES_PROFILE).mkdir(parents=True)
        self.executable = root / "hermes"
        self.executable.touch()
        self.engine = HermesProfileEngine(
            hermes_executable=self.executable,
            hermes_home=self.home,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @patch("app.engine.subprocess.Popen")
    def test_reply_uses_only_isolated_profile_environment_and_fixed_model(self, popen: Mock) -> None:
        process = Mock()
        process.stdout = io.StringIO(
            '{"type":"system","subtype":"init"}\n'
            '{"type":"text","text":"first line"}\n'
            '{"type":"text","text":"second line"}\n'
            '{"type":"result","exit_code":0}\n'
        )
        process.wait.return_value = 0
        process.poll.return_value = 0
        popen.return_value = process

        reply = self.engine.reply(room_id="opaque-room-id", prompt="A warm oak table")

        self.assertEqual(reply.content, "first linesecond line")
        command = popen.call_args.args[0]
        self.assertEqual(
            command,
            [
                str(self.executable),
                "chat",
                "-q",
                "A warm oak table",
                "--oneshot",
                "--quiet",
                "--format",
                "stream-json",
                "--toolsets",
                "web,vision,image_gen,memory",
                "--provider",
                DEFAULT_HERMES_PROVIDER,
                "--model",
                DEFAULT_HERMES_MODEL,
            ],
        )
        kwargs = popen.call_args.kwargs
        self.assertEqual(kwargs["cwd"], self.home / "profiles" / DEFAULT_HERMES_PROFILE)
        self.assertEqual(
            kwargs["env"],
            {
                "HERMES_HOME": str(self.home / "profiles" / DEFAULT_HERMES_PROFILE),
                "HERMES_BASE_HOME": str(self.home),
                "HOME": str(self.home / "profiles" / DEFAULT_HERMES_PROFILE),
                "PATH": f"{self.executable.parent}:/usr/bin:/bin",
                "PYTHONUTF8": "1",
                "NO_COLOR": "1",
                "TERM": "dumb",
            },
        )
        self.assertNotIn("opaque-room-id", command)
        self.assertEqual(kwargs["stderr"], __import__("subprocess").DEVNULL)

    @patch("app.engine.subprocess.Popen")
    def test_stream_reply_yields_incremental_chunks(self, popen: Mock) -> None:
        process = Mock()
        process.stdout = io.StringIO(
            '{"type":"text","text":"first"}\n'
            'not json\n'
            '{"type":"text","text":"second"}\n'
        )
        process.wait.return_value = 0
        process.poll.return_value = 0
        popen.return_value = process

        chunks = list(self.engine.stream_reply(room_id="opaque-room-id", prompt="Hello"))

        self.assertEqual([chunk.content for chunk in chunks], ["first", "second"])

    @patch("app.engine.subprocess.Popen")
    def test_engine_failure_does_not_return_child_stderr(self, popen: Mock) -> None:
        process = Mock()
        process.stdout = io.StringIO("")
        process.wait.return_value = 1
        process.poll.return_value = 1
        popen.return_value = process

        with self.assertRaisesRegex(EngineError, "did not complete") as error:
            list(self.engine.stream_reply(room_id="opaque-room-id", prompt="Hello"))

        self.assertNotIn("secret", str(error.exception).lower())

    def test_rejects_default_profile_but_accepts_configured_model_and_provider(self) -> None:
        with self.assertRaises(ValueError):
            HermesProfileEngine(
                hermes_executable=self.executable,
                hermes_home=self.home,
                profile="default",
            )
        configured = HermesProfileEngine(
            hermes_executable=self.executable,
            hermes_home=self.home,
            model="other-model",
            provider="example-provider",
        )
        self.assertEqual(configured._model, "other-model")
        self.assertEqual(configured._provider, "example-provider")


class FakeEngineTests(unittest.TestCase):
    def test_engine_does_not_claim_to_be_connected_to_hermes(self) -> None:
        reply = FakeEngine().reply(room_id="opaque-room-id", prompt="A warm oak table")
        self.assertIn("prototype", reply.content)
        self.assertIn("not connected", reply.content)
        self.assertNotIn("opaque-room-id", reply.content)

    def test_stream_reply_remains_available_for_tests(self) -> None:
        chunks = list(FakeEngine().stream_reply(room_id="opaque-room-id", prompt="A warm oak table"))
        self.assertEqual(len(chunks), 1)
        self.assertIn("prototype", chunks[0].content)


class UploadValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.upload_dir = Path(self.temp_dir.name) / "uploads"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_valid_png_is_stored_with_opaque_owner_only_name(self) -> None:
        stored = store_image(_tiny_png_bytes(), self.upload_dir)

        self.assertEqual(stored.mime_type, "image/png")
        self.assertEqual((stored.width, stored.height), (4, 3))
        path = self.upload_dir / stored.filename
        self.assertTrue(path.is_file())
        self.assertNotIn("photo", stored.filename.lower())
        self.assertLessEqual(path.stat().st_mode & 0o777, 0o600)

    def test_non_image_bytes_are_rejected_without_writing_a_file(self) -> None:
        with self.assertRaises(UploadValidationError):
            store_image(b"this is not an image at all", self.upload_dir)
        if self.upload_dir.exists():
            self.assertFalse(any(self.upload_dir.iterdir()))

    def test_contact_sheet_numbers_images_and_has_private_permissions(self) -> None:
        first = store_image(_tiny_png_bytes(), self.upload_dir)
        second_data = io.BytesIO()
        from PIL import Image
        Image.new("RGB", (5, 3), "navy").save(second_data, format="PNG")
        second = store_image(second_data.getvalue(), self.upload_dir)
        sheet = create_contact_sheet([self.upload_dir / first.filename, self.upload_dir / second.filename], self.upload_dir)
        with Image.open(sheet) as image:
            self.assertEqual(image.size, (2454, 942))
        self.assertLessEqual(sheet.stat().st_mode & 0o777, 0o600)

    def test_oversized_upload_is_rejected(self) -> None:
        with self.assertRaises(UploadValidationError):
            store_image(_tiny_png_bytes() + b"\x00" * (10 * 1024 * 1024), self.upload_dir)


class AttachmentStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "together.db")
        self.store.initialize()
        self.member_one = self.store.bootstrap_user("member_one", "Member One", hash_password("member_one-test-password"))
        self.member_two = self.store.bootstrap_user("member_two", "Member Two", hash_password("member_two-test-password"))
        self.room = self.store.create_shared_room("Shared conversation", [self.member_one.id, self.member_two.id])

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_attachment_binds_to_one_message_and_stays_member_only(self) -> None:
        attachment_id = self.store.create_attachment(self.room.public_id, self.member_one, "abc.png", "image/png", 4, 3)
        message = self.store.add_message(self.room.public_id, self.member_one, "user", "look at this", attachment_id)

        self.assertEqual(message["attachment"]["id"], attachment_id)
        with self.assertRaises(PermissionError):
            self.store.add_message(self.room.public_id, self.member_one, "user", "again", attachment_id)
        stranger = self.store.bootstrap_user("other", "Other", hash_password("other-test-password"))
        with self.assertRaises(PermissionError):
            self.store.attachment_for_member(attachment_id, stranger)

    def test_unbound_attachment_cannot_be_fetched(self) -> None:
        attachment_id = self.store.create_attachment(self.room.public_id, self.member_one, "abc.png", "image/png", 4, 3)
        with self.assertRaises(PermissionError):
            self.store.attachment_for_member(attachment_id, self.member_one)


class ApiV11Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        home = root / "engine-home"
        (home / "profiles" / DEFAULT_HERMES_PROFILE).mkdir(parents=True)
        executable = root / "hermes"
        executable.touch()
        settings = Settings(
            db_path=root / "together.db",
            static_dir=Path(__file__).resolve().parent.parent / "static",
            upload_dir=root / "uploads",
            session_secret="x" * 32,
            participants=TEST_PARTICIPANTS,
            cookie_secure=False,
            hermes_home=home,
            hermes_executable=executable,
        )
        self.app = create_app(settings, FakeEngine())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _login(client: TestClient, username: str) -> str:
        password = f"{username}-development-password"
        response = client.post("/api/auth/login", json={"username": username, "password": password})
        assert response.status_code == 200
        return response.json()["csrfToken"]

    def test_private_room_is_owner_only_end_to_end(self) -> None:
        with TestClient(self.app) as member_one_client, TestClient(self.app) as member_two_client:
            member_one_csrf = self._login(member_one_client, "member_one")
            created = member_one_client.post(
                "/api/rooms",
                json={"title": "My private notes", "visibility": "private"},
                headers={"X-Together-CSRF": member_one_csrf},
            )

            self.assertEqual(created.status_code, 201)
            room_id = created.json()["id"]
            self.assertIn(created.json(), member_one_client.get("/api/rooms").json())
            self.assertIn(room_id, [room["id"] for room in member_one_client.get("/api/rooms/activity").json()])

            member_two_csrf = self._login(member_two_client, "member_two")
            self.assertNotIn(created.json(), member_two_client.get("/api/rooms").json())
            self.assertNotIn(room_id, [room["id"] for room in member_two_client.get("/api/rooms/activity").json()])
            self.assertEqual(member_two_client.get(f"/api/rooms/{room_id}/messages").status_code, 404)
            upload = member_two_client.post(
                f"/api/rooms/{room_id}/attachments",
                files={"image": ("photo.png", _tiny_png_bytes(), "image/png")},
                headers={"X-Together-CSRF": member_two_csrf},
            )
            self.assertEqual(upload.status_code, 404)

    def test_room_activity_reports_latest_shared_message_id(self) -> None:
        with TestClient(self.app) as member_one_client, TestClient(self.app) as member_two_client:
            member_one_csrf = self._login(member_one_client, "member_one")
            member_two_csrf = self._login(member_two_client, "member_two")
            room_id = next(room["id"] for room in member_one_client.get("/api/rooms").json() if room["title"] == "Shared conversation")
            before = {room["id"]: room["latestMessageId"] for room in member_two_client.get("/api/rooms/activity").json()}
            sent = member_one_client.post(
                f"/api/rooms/{room_id}/messages",
                json={"content": "New activity marker test"},
                headers={"X-Together-CSRF": member_one_csrf},
            )
            self.assertEqual(sent.status_code, 200)
            after = {room["id"]: room["latestMessageId"] for room in member_two_client.get("/api/rooms/activity").json()}
            self.assertGreater(int(after[room_id]), int(before[room_id] or "0"))

    def test_rename_and_archive_room_via_api(self) -> None:
        with TestClient(self.app) as member_one_client, TestClient(self.app) as member_two_client:
            member_one_csrf = self._login(member_one_client, "member_one")
            created = member_one_client.post(
                "/api/rooms", json={"title": "Kitchen ideas"}, headers={"X-Together-CSRF": member_one_csrf}
            )
            room_id = created.json()["id"]

            renamed = member_one_client.patch(
                f"/api/rooms/{room_id}", json={"title": "Pantry plan"}, headers={"X-Together-CSRF": member_one_csrf}
            )
            self.assertEqual(renamed.status_code, 200)
            self.assertEqual(renamed.json()["title"], "Pantry plan")

            archived = member_one_client.post(f"/api/rooms/{room_id}/archive", headers={"X-Together-CSRF": member_one_csrf})
            self.assertEqual(archived.status_code, 204)
            self.assertNotIn(room_id, [room["id"] for room in member_one_client.get("/api/rooms").json()])

            member_two_csrf = self._login(member_two_client, "member_two")
            self.assertNotIn(room_id, [room["id"] for room in member_two_client.get("/api/rooms").json()])

    def test_image_upload_attach_and_serve_flow_in_shared_room(self) -> None:
        with TestClient(self.app) as member_one_client, TestClient(self.app) as member_two_client:
            member_one_csrf = self._login(member_one_client, "member_one")
            room_id = next(room["id"] for room in member_one_client.get("/api/rooms").json() if room["title"] == "Shared conversation")

            uploaded = member_one_client.post(
                f"/api/rooms/{room_id}/attachments",
                files={"image": ("photo.png", _tiny_png_bytes(), "image/png")},
                headers={"X-Together-CSRF": member_one_csrf},
            )
            self.assertEqual(uploaded.status_code, 201)
            attachment_id = uploaded.json()["id"]

            # Unbound uploads are not servable yet.
            self.assertEqual(member_one_client.get(f"/api/attachments/{attachment_id}/image").status_code, 404)

            sent = member_one_client.post(
                f"/api/rooms/{room_id}/messages",
                json={"content": "what is this?", "attachment_id": attachment_id},
                headers={"X-Together-CSRF": member_one_csrf},
            )
            self.assertEqual(sent.status_code, 200)
            self.assertEqual(sent.json()["userMessage"]["attachment"]["id"], attachment_id)

            image = member_one_client.get(f"/api/attachments/{attachment_id}/image")
            self.assertEqual(image.status_code, 200)
            self.assertEqual(image.headers["content-type"], "image/png")

            # Shared-room members can view the same image.
            self._login(member_two_client, "member_two")
            self.assertEqual(member_two_client.get(f"/api/attachments/{attachment_id}/image").status_code, 200)

    def test_invalid_image_upload_is_rejected_and_leaves_no_file(self) -> None:
        with TestClient(self.app) as member_one_client:
            member_one_csrf = self._login(member_one_client, "member_one")
            room_id = next(room["id"] for room in member_one_client.get("/api/rooms").json() if room["title"] == "Shared conversation")

            response = member_one_client.post(
                f"/api/rooms/{room_id}/attachments",
                files={"image": ("note.txt", b"hello world, not an image", "text/plain")},
                headers={"X-Together-CSRF": member_one_csrf},
            )

            self.assertEqual(response.status_code, 422)
            upload_dir = Path(self.temp_dir.name) / "uploads"
            if upload_dir.exists():
                self.assertFalse(any(upload_dir.iterdir()))


class HermesProfileEngineImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.home = root / "engine-home"
        (self.home / "profiles" / DEFAULT_HERMES_PROFILE).mkdir(parents=True)
        self.executable = root / "hermes"
        self.executable.touch()
        self.engine = HermesProfileEngine(
            hermes_executable=self.executable,
            hermes_home=self.home,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @patch("app.engine.subprocess.Popen")
    def test_stream_reply_passes_authorized_image_path_to_hermes(self, popen: Mock) -> None:
        process = Mock()
        process.stdout = io.StringIO('{"type":"text","text":"ok"}\n')
        process.wait.return_value = 0
        process.poll.return_value = 0
        popen.return_value = process

        image_path = Path("/isolated/engine-home/profiles/together/uploads/abc123.png")
        list(self.engine.stream_reply(room_id="opaque-room-id", prompt="describe this", image_path=image_path))

        command = popen.call_args.args[0]
        self.assertEqual(command[-2:], ["--image", str(image_path)])

    @patch("app.engine.subprocess.Popen")
    def test_generated_image_result_is_captured_only_from_isolated_cache(self, popen: Mock) -> None:
        generated = self.home / "profiles" / DEFAULT_HERMES_PROFILE / "cache" / "images" / "generated.png"
        generated.parent.mkdir(parents=True)
        generated.write_bytes(_tiny_png_bytes())
        output = json.dumps({"success": True, "image": str(generated)})
        process = Mock()
        process.stdout = io.StringIO(
            '{"type":"tool_result","name":"image_generate","output":' + json.dumps(output) + '}\n'
            '{"type":"text","text":"/private/cache/generated.png"}\n'
        )
        process.wait.return_value = 0
        process.poll.return_value = 0
        popen.return_value = process

        chunks = list(self.engine.stream_reply(room_id="opaque-room-id", prompt="Generate an image"))

        self.assertEqual(chunks[-1].content, "Here you go — I made that image.")
        self.assertEqual(chunks[-1].generated_image_path, generated)
        self.assertNotIn("/private/cache", "".join(chunk.content for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
