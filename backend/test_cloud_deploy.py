from __future__ import annotations

import hashlib
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.core.config import LLMProviderConfig
from backend.core import cloud_deploy
from backend.core.connection_string import make_connection_string
from deploy.update_feishu import _pending_open_id, read_status, update_config
from deploy.update_hub_env import update_environment


class CloudDeployTests(unittest.TestCase):
    def test_desktop_model_sync_clears_only_the_browser_model_override(self):
        from deploy.update_hub_env import reset_saved_model_settings
        import os
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            text = ('[llm]\nsettings_saved = true\nchat_model = "browser"\n'
                    '[auth]\npassword = "keep-private"\nsettings_saved = true\n')
            path.write_text(text, encoding="utf-8")
            reset_saved_model_settings(path)
            self.assertEqual(text.replace("settings_saved = true", "settings_saved = false", 1), path.read_text(encoding="utf-8"))
            self.assertEqual([path], list(Path(tmp).iterdir()))
            if os.name != "nt":
                self.assertEqual(0o600, path.stat().st_mode & 0o777)
            # Existing or missing configs without a browser override are untouched.
            path.write_text('[llm]\nchat_model = "old"\n', encoding="utf-8")
            reset_saved_model_settings(path)
            self.assertEqual('[llm]\nchat_model = "old"\n', path.read_text(encoding="utf-8"))
            reset_saved_model_settings(path.with_name("absent.toml"))

    def test_model_sync_command_resets_override_but_password_sync_does_not(self):
        from deploy import update_hub_env
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = root / "hub.env"
            env.write_text("AGENT_ROLE=hub\n", encoding="utf-8")
            payload = root / "input.json"
            script = root / "deploy" / "update_hub_env.py"
            for values, reset_expected in (({"AGENT_CHAT_MODEL": "synced"}, True),
                                           ({"AGENT_WEB_PASSWORD": "replacement"}, False)):
                payload.write_text(json.dumps(values), encoding="utf-8")
                with patch.object(update_hub_env, "__file__", str(script)), \
                     patch("sys.argv", [str(script), str(payload), str(env)]), \
                     patch.object(update_hub_env, "reset_saved_model_settings") as reset:
                    update_hub_env.main()
                    if reset_expected:
                        reset.assert_called_once_with(root / "config.toml")
                    else:
                        reset.assert_not_called()
                self.assertIn(next(iter(values)), env.read_text(encoding="utf-8"))

    def test_feishu_status_exposes_configuration_without_secret(self):
        status = read_status(
            '[feishu]\napp_id = "cli_cloud"\napp_secret = "private"\n'
            'allowed_open_ids = ["ou_one", ""]\n'
        )
        self.assertEqual(
            {"app_id": "cli_cloud", "allowed_open_ids": ["ou_one"], "app_secret_configured": True},
            status,
        )
        self.assertNotIn("private", json.dumps(status))

    def test_feishu_status_distinguishes_missing_record_from_read_error(self):
        config = '[feishu]\napp_id = "app-one"\napp_secret = "private"\nallowed_open_ids = ["trusted"]\n'
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "recent.json"
            status = read_status(config, state)
            self.assertEqual("", status["last_open_id"])
            self.assertEqual("unrecorded", status["recent_status"])
            state.write_text(json.dumps({"open_id": "trusted", "app_id": "app-one"}), encoding="utf-8")
            status = read_status(config, state)
            self.assertEqual("trusted", status["last_open_id"])
            self.assertEqual("recorded", status["recent_status"])
            self.assertNotIn("private", json.dumps(status))
            for value in [{"open_id": "stranger"}, {"open_id": "trusted", "app_id": "old-app"}, {"open_id": None}]:
                state.write_text(json.dumps(value), encoding="utf-8")
                self.assertEqual("", read_status(config, state)["last_open_id"])
            for value in ["{", "null", "[]"]:
                state.write_text(value, encoding="utf-8")
                status = read_status(config, state)
                self.assertEqual("unavailable", status["recent_status"])
                self.assertEqual("", status["last_open_id"])

    def _payload(self, root: Path) -> None:
        (root / "backend" / "__pycache__").mkdir(parents=True)
        (root / "backend" / "module.py").write_text("value = 1\n", encoding="utf-8")
        (root / "backend" / "__pycache__" / "module.pyc").write_bytes(b"cache")
        (root / "deploy").mkdir()
        (root / "deploy" / "deploy_hub.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (root / "deploy" / "cloud_transaction.py").write_text("# transaction\n", encoding="utf-8")
        (root / "frontend/src").mkdir(parents=True)
        (root / "frontend/src/settings.html").write_text("<html></html>", encoding="utf-8")
        (root / "config.example.toml").write_text("[feishu]\n", encoding="utf-8")

    def test_target_validation_distinguishes_bad_ip_and_username(self):
        self.assertEqual(("203.0.113.10", "root"), cloud_deploy.validate_target("203.0.113.10", "root"))
        with self.assertRaisesRegex(ValueError, "公网 IP"):
            cloud_deploy.validate_target("not-an-ip", "root")
        with self.assertRaisesRegex(ValueError, "用户名"):
            cloud_deploy.validate_target("203.0.113.10", "bad user")

    def test_packaged_payload_uses_shared_config_and_frontend_assets(self):
        # Mirror deploy/xiaoa_backend.spec, not the development checkout layout.
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            bundled = assets / "server-payload"
            self._payload(bundled)
            (bundled / "config.example.toml").rename(assets / "config.example.toml")
            (bundled / "frontend").rename(assets / "frontend")
            output = assets / "payload.tar.gz"
            with patch.object(cloud_deploy, "asset_root", return_value=assets):
                cloud_deploy._archive_payload(output)
            with tarfile.open(output) as archive:
                names = archive.getnames()
                self.assertIn("config.example.toml", names)
                self.assertIn("frontend/src/settings.html", names)
                self.assertIn("deploy/cloud_transaction.py", names)
                self.assertEqual((assets / "config.example.toml").read_bytes(),
                                 archive.extractfile("config.example.toml").read())

    def test_payload_archive_excludes_python_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._payload(root)
            output = root / "payload.tar.gz"
            (root / "config.toml").write_text("private-key", encoding="utf-8")
            (root / "data" / "cloud-ssh").mkdir(parents=True)
            (root / "data" / "cloud-ssh" / "identity.key").write_text("private-ssh", encoding="utf-8")
            (root / "data" / "chat.json").write_text("private-chat", encoding="utf-8")
            with patch.object(cloud_deploy, "_payload_root", return_value=root):
                cloud_deploy._archive_payload(output)
            with tarfile.open(output) as archive:
                names = archive.getnames()
            self.assertIn("backend/module.py", names)
            self.assertIn("frontend/src/settings.html", names)
            self.assertNotIn("backend/__pycache__/module.pyc", names)
            self.assertNotIn("config.toml", names)
            self.assertFalse(any(name == "data" or name.startswith("data/") for name in names))

    def test_apply_weixin_uploads_script_for_existing_cloud_servers(self):
        fake = MagicMock()
        script_content = "print('update weixin')\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "deploy").mkdir()
            (root / "deploy" / "update_weixin.py").write_text(script_content, encoding="utf-8")
            with patch.object(cloud_deploy, "_payload_root", return_value=root):
                cloud_deploy._apply_weixin(
                    fake, "", {"allowed_user_ids": ["wxid_user"]}, restart=False,
                )

        remote_script = fake.write_text.call_args_list[0].args[0]
        remote_payload = fake.write_text.call_args_list[1].args[0]
        command = fake.run.call_args.args[0]
        self.assertTrue(remote_script.startswith("/tmp/xiaoa-weixin-update-"))
        self.assertTrue(remote_script.endswith(".py"))
        self.assertIn(script_content, fake.write_text.call_args_list[0].args[1])
        self.assertIn(remote_script, command)
        self.assertIn(remote_payload, command)
        self.assertIn(f"rm -f {remote_script} {remote_payload}", command)
        self.assertNotIn(f"{cloud_deploy.REMOTE_ROOT}/deploy/update_weixin.py", command)

    def test_project_maintenance_uses_hub_runtime_and_no_listener_restart(self):
        fake = MagicMock()
        cloud_deploy._apply_weixin(fake, "sudo -n ", {"project_library": {"action": "save"}}, restart=False)
        command = fake.run.call_args.args[0]
        self.assertIn("sudo -n /opt/xiaoa/.venv/bin/python", command)
        self.assertNotIn("systemctl", command)
        with patch.object(cloud_deploy, "_read_weixin_response", return_value={"ok": True}) as read:
            self.assertTrue(cloud_deploy.get_project_libraries(Path("test-root"))["ok"])
            self.assertEqual("projects", read.call_args.args[1])

    def test_desktop_project_payload_uses_private_temp_file_without_restart(self):
        fake = MagicMock()
        fake.run.return_value = '{"ok":true,"context":null}'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data/cloud-ssh").mkdir(parents=True)
            (root / "data/cloud-ssh/target.json").write_text('{"host":"203.0.113.10","username":"admin"}')
            key = root / "id.key"
            key.touch()
            with patch.object(cloud_deploy, "identity_path", return_value=key), \
                 patch.object(cloud_deploy, "SSHClient", return_value=fake), \
                 patch.object(cloud_deploy, "_elevation", return_value=""):
                client = fake.__enter__.return_value
                client.run.return_value = '{"ok":true,"context":null}'
                result = cloud_deploy.desktop_project_memory(root, {"text":"火箭项目 保存：合同"})
        self.assertTrue(result["ok"])
        command = client.run.call_args.args[0]
        self.assertIn("--project-memory", command)
        self.assertNotIn("合同", command)
        self.assertNotIn("systemctl", command)
        payload_path, payload = client.write_text.call_args_list[1].args
        self.assertIn(payload_path, command)
        self.assertEqual("火箭项目 保存：合同", json.loads(payload)["text"])

    def test_weixin_remarks_reload_listener_even_without_requested_restart(self):
        fake = MagicMock()
        for remark in ("张三", ""):
            with self.subTest(remark=remark):
                cloud_deploy._apply_weixin(
                    fake, "sudo -n ", {"account_id": "bot-a", "remark": remark}, restart=False,
                )
                command = fake.run.call_args.args[0]
                stop = command.index("sudo -n systemctl stop xiaoa-hub")
                update = command.index("sudo -n python3")
                start = command.index("sudo -n systemctl start xiaoa-hub")
                self.assertLess(stop, update, "stop stale pollers before updating their state file")
                self.assertLess(update, start, "reload the saved remark before polling resumes")
                self.assertIn("if [ $code -eq 0 ]; then", command)

    def test_apply_feishu_uploads_current_script_for_logout(self):
        fake = MagicMock()
        script_content = "print('update feishu')\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "deploy").mkdir()
            (root / "deploy" / "update_feishu.py").write_text(script_content, encoding="utf-8")
            with patch.object(cloud_deploy, "_payload_root", return_value=root):
                cloud_deploy._apply_feishu(
                    fake, "", {"logout": True, "allowed_open_ids": []}, restart=True,
                )

        remote_script = fake.write_text.call_args_list[0].args[0]
        remote_payload = fake.write_text.call_args_list[1].args[0]
        command = fake.run.call_args.args[0]
        self.assertTrue(remote_script.startswith("/tmp/xiaoa-feishu-update-"))
        self.assertTrue(remote_script.endswith(".py"))
        self.assertIn(script_content, fake.write_text.call_args_list[0].args[1])
        self.assertIn(remote_script, command)
        self.assertIn(remote_payload, command)
        self.assertIn("systemctl stop xiaoa-hub", command)
        self.assertIn("systemctl start xiaoa-hub", command)
        self.assertIn(f"rm -f {remote_script} {remote_payload}", command)
        self.assertNotIn(f"{cloud_deploy.REMOTE_ROOT}/deploy/update_feishu.py", command)

    def test_deploy_uses_key_after_enrollment_and_deletes_remote_credentials(self):
        fingerprint = hashlib.sha256(b"cert").hexdigest()
        connection = make_connection_string("203.0.113.10", 1115, fingerprint, "x" * 32)
        fake = MagicMock()
        fake.__enter__.return_value = fake

        def run(command, **_kwargs):
            if command == "id -u":
                return "0\n"
            if "cat /opt/xiaoa/data/connection-string.txt" in command:
                return f"XIAOA_CONNECTION_STRING={connection}\nAGENT_WEB_PASSWORD={'w' * 32}\n"
            return ""

        fake.run.side_effect = run
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._payload(root)
            progress = []
            with patch.object(cloud_deploy, "_payload_root", return_value=root), \
                 patch.object(cloud_deploy, "enroll", return_value={"path": str(root / "key")}), \
                 patch.object(cloud_deploy, "SSHClient", return_value=fake) as ssh_type, \
                 patch.object(cloud_deploy, "_save_local_credentials"):
                result = cloud_deploy.deploy(
                    "203.0.113.10", "root", "root-password", root,
                    llm_protocol="responses",
                    web_password="short-pass",
                    llm_providers=[LLMProviderConfig(
                        id="backup", base_url="https://backup.invalid/v1", api_key="backup-secret",
                        models=["backup-first", "backup-second"], protocol="anthropic")],
                    progress=lambda stage, message: progress.append((stage, message)),
                )
        self.assertEqual("203.0.113.10", result["connection"]["host"])
        ssh_type.assert_called_once_with("203.0.113.10", "root", key_filename=str(root / "key"))
        fake.upload.assert_called_once()
        fake.run_stream.assert_called_once()
        settings = next(json.loads(call.args[1]) for call in fake.write_text.call_args_list if ".json" in call.args[0])
        self.assertEqual("responses", settings["runtime"]["AGENT_LLM_PROTOCOL"])
        self.assertEqual("short-pass", settings["runtime"]["AGENT_WEB_PASSWORD"])
        providers = json.loads(settings["runtime"]["AGENT_LLM_PROVIDERS"])
        self.assertEqual(["backup-first", "backup-second"], providers[0]["models"])
        self.assertEqual("anthropic", providers[0]["protocol"])
        self.assertNotIn("backup-secret", settings["runtime"]["AGENT_LLM_PROVIDERS"])
        self.assertEqual({"backup": "backup-secret"}, json.loads(settings["runtime"]["AGENT_LLM_PROVIDER_API_KEYS"]))
        self.assertNotIn("AGENT_LLM_API_KEY", settings["runtime"])
        self.assertTrue(any("rm -f /opt/xiaoa/data/connection-string.txt" in call.args[0] for call in fake.run.call_args_list))
        self.assertIn("--digest", fake.run_stream.call_args.args[0])
        self.assertEqual("ssh", progress[0][0])

    def test_remote_install_error_uses_summary_and_keeps_cleanup(self):
        fake = MagicMock()
        fake.__enter__.return_value = fake
        fake.run.side_effect = lambda command, **kwargs: '0' if command == 'id -u' else ''
        fake.run_stream.side_effect = cloud_deploy.SSHClientError(
            '远程命令退出码 1: XIAOA_PROGRESS {"stage":"runtime","message":"安装依赖"}\n'
            'XIAOA_PROGRESS {"stage":"rollback","message":"已回退"}\n'
            'XIAOA_ERROR 服务器 APT 存在未满足依赖（退出码 100）')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._payload(root)
            with patch.object(cloud_deploy, '_payload_root', return_value=root), \
                 patch.object(cloud_deploy, 'enroll', return_value={'path': str(root / 'key')}), \
                 patch.object(cloud_deploy, 'SSHClient', return_value=fake):
                with self.assertRaises(cloud_deploy.SSHClientError) as caught:
                    cloud_deploy.deploy('203.0.113.10', 'root', '', root)
        self.assertEqual('服务器 APT 存在未满足依赖（退出码 100）', str(caught.exception))
        self.assertTrue(any('rm -f /tmp/xiaoa-' in call.args[0] for call in fake.run.call_args_list))

    def test_byte_callback_and_commit_are_structured_and_credentials_never_become_progress(self):
        connection = make_connection_string("203.0.113.10", 1115, "f" * 64, "t" * 32)
        fake = MagicMock()
        fake.__enter__.return_value = fake
        fake.run.side_effect = lambda command, **kwargs: "0" if command == "id -u" else (f"XIAOA_CONNECTION_STRING={connection}\nAGENT_WEB_PASSWORD={'w' * 32}\n" if "cat /opt/xiaoa/data/connection-string.txt" in command else "")
        def upload(_source, _target, **kwargs):
            kwargs["callback"](5, 10)
            kwargs["callback"](10, 10)
        fake.upload.side_effect = upload
        def stream(_command, callback, **kwargs):
            callback('XIAOA_PROGRESS {"stage":"health","message":"healthy","cloud_re')
            callback('ady":true}\nsecret arbitrary output\n')
            return 'secret arbitrary output\nXIAOA_RESULT ' + json.dumps({
                'healthy': True, 'version': '0.9.6', 'port': 1115,
                'private': 'must not be retained',
            }) + '\n'
        fake.run_stream.side_effect = stream
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._payload(root)
            events = []
            with patch.object(cloud_deploy, "_payload_root", return_value=root), \
                 patch.object(cloud_deploy, "enroll", return_value={"path": str(root / "key")}), \
                 patch.object(cloud_deploy, "SSHClient", return_value=fake), \
                 patch.object(cloud_deploy, "_save_local_credentials"):
                result = cloud_deploy.deploy("203.0.113.10", "root", "", root,
                                             progress=lambda stage, message, **extra: events.append({"stage": stage, "message": message, **extra}))
        self.assertTrue(any(event.get("uploaded_bytes") == 5 and event["total_bytes"] == 10 for event in events))
        self.assertTrue(any(event.get("cloud_ready") for event in events))
        self.assertNotIn("secret arbitrary output", json.dumps(events))
        self.assertNotIn(connection, json.dumps(events))
        self.assertEqual({"healthy": True, "port": 1115, "version": "0.9.6"}, result["remote_status"])

    def test_remote_result_parser_ignores_untrusted_fields_and_malformed_output(self):
        output = 'noise\nXIAOA_RESULT ' + json.dumps({
            'healthy': True, 'version': 'v' * 100, 'port': 1115,
            'secret': 'not retained', 'data_preserved': False,
        })
        self.assertEqual({
            'healthy': True, 'version': 'v' * 80, 'port': 1115, 'data_preserved': False,
        }, cloud_deploy._parse_remote_result(output))
        for invalid in (None, '', 'noise', 'XIAOA_RESULT {bad', 'XIAOA_RESULT []'):
            with self.subTest(output=invalid):
                self.assertEqual({}, cloud_deploy._parse_remote_result(invalid))

    def test_maintenance_is_read_only_to_ssh_keys_and_secret_recovery_does_not_deploy(self):
        fake = MagicMock()
        connection = make_connection_string("203.0.113.10", 1115, "f" * 64, "t" * 32)
        fake.run.side_effect = lambda command, **kwargs: "0" if command == "id -u" else (json.dumps({"connection_string": connection, "web_password": "w" * 32}) if " credentials" in command else "")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._payload(root)
            key = root / "key"
            key.write_text("key", encoding="utf-8")
            with patch.object(cloud_deploy, "_payload_root", return_value=root), \
                 patch.object(cloud_deploy, "identity_path", return_value=key), \
                 patch.object(cloud_deploy, "SSHClient", return_value=fake), \
                 patch.object(cloud_deploy, "enroll") as enroll, \
                 patch.object(cloud_deploy, "_save_local_credentials") as save:
                result = cloud_deploy.recover_connection("203.0.113.10", "root", "", root)
        self.assertEqual(connection, result["connection_string"])
        enroll.assert_not_called()
        fake.upload.assert_not_called()
        save.assert_called_once()
        self.assertFalse(any("rotate-token" in call.args[0] or "deploy_hub.sh" in call.args[0] for call in fake.run.call_args_list))

    def test_credential_parser_rejects_incomplete_file(self):
        with self.assertRaisesRegex(Exception, "连接串"):
            cloud_deploy._parse_credentials("AGENT_WEB_PASSWORD=long-enough-password")

    def test_credential_parser_accepts_legacy_nonempty_web_password(self):
        connection = make_connection_string("203.0.113.10", 1115, "f" * 64, "t" * 32)
        result = cloud_deploy._parse_credentials(
            f"XIAOA_CONNECTION_STRING={connection}\nAGENT_WEB_PASSWORD=oldpass1234\n"
        )
        self.assertEqual("oldpass1234", result["web_password"])

    def test_credential_parser_rejects_missing_web_password(self):
        connection = make_connection_string("203.0.113.10", 1115, "f" * 64, "t" * 32)
        with self.assertRaisesRegex(Exception, "网页登录密码"):
            cloud_deploy._parse_credentials(f"XIAOA_CONNECTION_STRING={connection}\n")


class FeishuConfigUpdateTests(unittest.TestCase):
    def test_pending_feishu_authorization_requires_current_unexpired_candidate(self):
        now = 1_800_000_000
        with patch("deploy.update_feishu.time.time", return_value=now):
            pending = {"open_id": "ou_pending", "app_id": "cli_app", "seen_at": now - 60}
            self.assertEqual("ou_pending", _pending_open_id(pending, "cli_app", ["ou_existing"]))
            for seen_at in (now - 601, now + 1):
                self.assertEqual("", _pending_open_id(
                    {**pending, "seen_at": seen_at}, "cli_app", ["ou_existing"],
                ))
            self.assertEqual("", _pending_open_id(pending, "other-app", []))
            self.assertEqual("", _pending_open_id(pending, "cli_app", ["ou_pending"]))

    def test_updates_only_supplied_feishu_fields(self):
        original = (
            '[llm]\nbase_url = "x"\n\n[feishu]\napp_id = "old"\n'
            'app_secret = "keep"\nallowed_open_ids = []\n\n[auth]\npassword = "x"\n'
        )
        updated = update_config(original, {"app_id": "cli_new", "allowed_open_ids": ["ou_one"]})
        self.assertIn('app_id = "cli_new"', updated)
        self.assertIn('app_secret = "keep"', updated)
        self.assertIn('allowed_open_ids = ["ou_one"]', updated)
        self.assertIn('[auth]\npassword = "x"', updated)

    def test_updates_one_feishu_remark_without_touching_other_fields(self):
        original = (
            '[feishu]\napp_id = "old"\napp_secret = "keep"\n'
            'allowed_open_ids = ["ou_one", "ou_two"]\n'
            'remarks = {"ou_two" = "原备注"}\n'
        )
        updated = update_config(original, {"remark_open_id": "ou_one", "remark": "张三"})
        self.assertIn('app_secret = "keep"', updated)
        self.assertIn('remarks = {"ou_two" = "原备注", "ou_one" = "张三"}', updated)


class HubEnvironmentUpdateTests(unittest.TestCase):
    def test_web_password_accepts_short_and_empty_values_without_shell_injection(self):
        import shlex
        from backend.core.config import Config, apply_env_overrides
        for password in ("short-pass", "复杂' $(echo unused) 密码", ""):
            with self.subTest(password_length=len(password)):
                updated = update_environment("AGENT_ROLE=hub\nAGENT_WEB_PASSWORD=old\n", {"AGENT_WEB_PASSWORD": password})
                tokens = shlex.split(updated.splitlines()[-1])
                self.assertEqual(["AGENT_WEB_PASSWORD=" + password], tokens)
                cfg = Config()
                cfg.role.mode = "hub"
                cfg.auth.password = "stale-config-password"
                with patch.dict("os.environ", {"AGENT_WEB_PASSWORD": password}):
                    apply_env_overrides(cfg)
                self.assertEqual(password, cfg.auth.password)
        for password in ("bad\nvalue", "bad\rvalue", "bad\0value"):
            with self.assertRaises(ValueError):
                update_environment("", {"AGENT_WEB_PASSWORD": password})

    def test_updates_only_allowed_model_environment(self):
        original = "AGENT_ROLE=hub\nAGENT_LLM_API_KEY=old\n"
        updated = update_environment(original, {
            "AGENT_LLM_PROTOCOL": "anthropic",
            "AGENT_LLM_BASE_URL": "https://api.example.com/v1",
            "AGENT_LLM_API_KEY": "new key",
            "AGENT_CHAT_MODEL": "chat-model",
            "AGENT_MODEL_OPTIONS": "chat-model,vision-model",
        })
        self.assertIn("AGENT_ROLE=hub", updated)
        self.assertIn("AGENT_LLM_PROTOCOL=anthropic", updated)
        self.assertIn("AGENT_LLM_API_KEY='new key'", updated)
        self.assertIn("AGENT_MODEL_OPTIONS=chat-model,vision-model", updated)
        with self.assertRaisesRegex(ValueError, "不允许"):
            update_environment(original, {"AGENT_HUB_TOKEN": "changed"})

    def test_removes_deprecated_vision_environment(self):
        updated = update_environment(
            "AGENT_CHAT_MODEL=chat-model\nAGENT_VISION_MODEL=deleted-model\n",
            {"AGENT_CHAT_MODEL": "chat-model"},
        )
        self.assertNotIn("AGENT_VISION_MODEL=", updated)

    def test_updates_tencent_voice_environment(self):
        updated = update_environment("AGENT_ROLE=hub\n", {
            "AGENT_TENCENT_SECRET_ID": "voice id",
            "AGENT_TENCENT_SECRET_KEY": "voice key",
            "AGENT_TENCENT_REGION": "ap-guangzhou",
            "AGENT_TTS_VOICE": "601003",
            "AGENT_TTS_SPEED": "0",
        })
        self.assertIn("AGENT_TENCENT_SECRET_ID='voice id'", updated)
        self.assertIn("AGENT_TENCENT_REGION=ap-guangzhou", updated)
        self.assertIn("AGENT_TTS_VOICE=601003", updated)

    def test_updates_baidu_voice_environment_without_changing_tencent(self):
        values = {
            "AGENT_SPEECH_PROVIDER": "baidu",
            "AGENT_BAIDU_API_KEY": "example-api",
            "AGENT_BAIDU_SECRET_KEY": "example-secret",
            "AGENT_BAIDU_TTS_VOICE": "0",
            "AGENT_BAIDU_TTS_SPEED": "5",
        }
        updated = update_environment("AGENT_TENCENT_SECRET_ID=existing-example\n", values)
        for key, value in values.items():
            self.assertIn(f"{key}={value}\n", updated)
        self.assertIn("AGENT_TENCENT_SECRET_ID=existing-example\n", updated)

    def test_explicitly_clears_tencent_credentials_without_touching_baidu(self):
        updated = update_environment(
            "AGENT_TENCENT_SECRET_ID=old-id\nAGENT_TENCENT_SECRET_KEY=old-key\n"
            "AGENT_BAIDU_API_KEY=baidu-example\n",
            {"AGENT_CLEAR_TENCENT_CREDENTIALS": "1", "AGENT_TENCENT_SECRET_ID": "", "AGENT_TENCENT_SECRET_KEY": ""},
        )
        self.assertNotIn("AGENT_TENCENT_SECRET_ID=", updated)
        self.assertNotIn("AGENT_TENCENT_SECRET_KEY=", updated)
        self.assertIn("AGENT_BAIDU_API_KEY=baidu-example", updated)
        with self.assertRaisesRegex(ValueError, "值无效"):
            update_environment("", {"AGENT_CLEAR_TENCENT_CREDENTIALS": "yes"})

    def test_allowed_includes_tencent_voice_and_schedule(self):
        from deploy.update_hub_env import ALLOWED
        self.assertIn("AGENT_TENCENT_SECRET_ID", ALLOWED)
        self.assertIn("AGENT_TENCENT_SECRET_KEY", ALLOWED)
        self.assertIn("AGENT_TTS_VOICE", ALLOWED)
        self.assertNotIn("AGENT_VOICE_PROVIDER", ALLOWED)
        self.assertIn("AGENT_SCHEDULE_TASKS", ALLOWED)
        self.assertIn("AGENT_LLM_PROTOCOL", ALLOWED)



class RuntimeSyncProbeTests(unittest.TestCase):
    def test_password_sync_uses_private_payload_and_caches_only_after_success(self):
        fake = MagicMock()
        fake.__enter__.return_value = fake
        fake.run.return_value = "0"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "data/cloud-ssh/target.json"
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps({"host": "203.0.113.10", "username": "root"}), encoding="utf-8")
            key = root / "key"
            key.touch()
            with patch.object(cloud_deploy, "identity_path", return_value=key), \
                 patch.object(cloud_deploy, "SSHClient", return_value=fake):
                cloud_deploy.sync_web_password(root, "short-pass")
                payload = next(json.loads(call.args[1]) for call in fake.write_text.call_args_list if call.args[0].endswith('.json'))
                self.assertEqual({"AGENT_WEB_PASSWORD": "short-pass"}, payload)
                self.assertNotIn("short-pass", str(fake.run.call_args_list))
                self.assertEqual("short-pass\n", (target.parent / "web-password.txt").read_text(encoding="utf-8"))
                fake.run.side_effect = cloud_deploy.SSHClientError("unavailable")
                with self.assertRaises(cloud_deploy.SSHClientError):
                    cloud_deploy.sync_web_password(root, "new-pass")
                self.assertEqual("short-pass\n", (target.parent / "web-password.txt").read_text(encoding="utf-8"))

    def test_remote_env_capabilities_parses_allowed_set(self):
        fake = MagicMock()
        fake.run.return_value = "AGENT_LLM_BASE_URL AGENT_CHAT_MODEL\n"
        self.assertEqual(
            {"AGENT_LLM_BASE_URL", "AGENT_CHAT_MODEL"},
            cloud_deploy._remote_env_capabilities(fake, ""),
        )

    def test_remote_env_capabilities_returns_none_on_failure(self):
        fake = MagicMock()
        fake.run.side_effect = cloud_deploy.SSHClientError("探测失败")
        self.assertIsNone(cloud_deploy._remote_env_capabilities(fake, ""))

    def test_sync_runtime_settings_rejects_unknown_fields_with_friendly_error(self):
        fake = MagicMock()
        fake.__enter__.return_value = fake
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data" / "cloud-ssh").mkdir(parents=True)
            (root / "data" / "cloud-ssh" / "target.json").write_text(
                json.dumps({"host": "203.0.113.10", "username": "root"}), encoding="utf-8",
            )
            key = root / "data" / "cloud-ssh" / "id"
            key.write_text("key", encoding="utf-8")
            with patch.object(cloud_deploy, "identity_path", return_value=key), \
                 patch.object(cloud_deploy, "SSHClient", return_value=fake), \
                 patch.object(cloud_deploy, "_remote_env_capabilities", return_value={"AGENT_LLM_BASE_URL"}), \
                 patch.object(cloud_deploy, "_apply_hub_environment"):
                with self.assertRaisesRegex(cloud_deploy.SSHClientError, "云端脚本版本较旧"):
                    cloud_deploy.sync_runtime_settings(root, {"AGENT_VOICE_PROVIDER": "grok"})

    def test_get_feishu_status_reads_remote_sanitized_status(self):
        fake = MagicMock()
        fake.__enter__.return_value = fake
        fake.run.side_effect = ["0", json.dumps({
            "app_id": "cli_cloud", "allowed_open_ids": ["ou_one"],
            "app_secret_configured": True,
        })]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data" / "cloud-ssh").mkdir(parents=True)
            (root / "data" / "cloud-ssh" / "target.json").write_text(
                json.dumps({"host": "203.0.113.10", "username": "root"}), encoding="utf-8",
            )
            key = root / "data" / "cloud-ssh" / "id"
            key.write_text("key", encoding="utf-8")
            with patch.object(cloud_deploy, "identity_path", return_value=key), \
                 patch.object(cloud_deploy, "SSHClient", return_value=fake):
                status = cloud_deploy.get_feishu_status(root)
        self.assertTrue(status["connected"])
        self.assertNotIn("app_secret", status)
        self.assertIn("--status", fake.run.call_args.args[0])
        fake.write_text.assert_called_once()


if __name__ == "__main__":
    unittest.main()
