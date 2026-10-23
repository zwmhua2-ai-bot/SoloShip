"""P03-D credential isolation tests: fake tokens only, never real ones."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bridge"))

import codex_adapter as codex  # noqa: E402
import env_guard  # noqa: E402
import p02_support as support  # noqa: E402
import github_publisher as gp  # noqa: E402

FAKE_CODEX = str(ROOT / "tests" / "fake_codex.py")
FAKE_TOKEN = "gho_FAKE_TOKEN_FOR_TEST_ONLY"


class EnvGuardTests(unittest.TestCase):
    def test_scrub_removes_known_and_patterned_names(self):
        env = {"PATH": "/usr/bin", "GH_TOKEN": FAKE_TOKEN, "GITHUB_TOKEN": FAKE_TOKEN,
               "MY_SECRET": "x", "SOME_API_KEY": "x", "OPENAI_BASE_URL": "https://x",
               "PYTHONUTF8": "1"}
        scrubbed, removed = env_guard.scrub_secrets(env)
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "MY_SECRET", "SOME_API_KEY"):
            self.assertNotIn(name, scrubbed)
        self.assertIn("PATH", scrubbed)
        self.assertIn("PYTHONUTF8", scrubbed)
        self.assertIn("GITHUB_TOKEN", removed)

    def test_scrub_is_not_in_place(self):
        env = {"GH_TOKEN": FAKE_TOKEN}
        scrubbed, _ = env_guard.scrub_secrets(env)
        self.assertIn("GH_TOKEN", env)
        self.assertNotIn("GH_TOKEN", scrubbed)

    def test_has_secret_lists_names_only(self):
        names = env_guard.has_secret({"GH_TOKEN": FAKE_TOKEN, "OTHER": "1"})
        self.assertEqual(names, ["GH_TOKEN"])

    def test_codex_home_stripped_by_default(self):
        """默认剥离：普通子进程拿不到 CODEX_HOME。"""
        env = {"CODEX_HOME": r"C:\fake\codex-home", "PATH": "/usr/bin"}
        scrubbed, removed = env_guard.scrub_secrets(env)
        self.assertNotIn("CODEX_HOME", scrubbed)
        self.assertEqual(removed, ["CODEX_HOME"])

    def test_codex_home_kept_only_when_explicitly_allowed(self):
        env = {"CODEX_HOME": r"C:\fake\codex-home", "GH_TOKEN": FAKE_TOKEN, "PATH": "/usr/bin"}
        scrubbed, removed = env_guard.scrub_secrets(env, keep_paths=env_guard.PATH_ENV_NAMES)
        self.assertEqual(scrubbed["CODEX_HOME"], r"C:\fake\codex-home")
        self.assertNotIn("GH_TOKEN", scrubbed)
        self.assertEqual(removed, ["GH_TOKEN"])

    def test_keep_paths_cannot_whitelist_a_credential(self):
        env = {"GH_TOKEN": FAKE_TOKEN, "CODEX_API_KEY": FAKE_TOKEN}
        scrubbed, removed = env_guard.scrub_secrets(env, keep_paths=("GH_TOKEN", "CODEX_API_KEY"))
        self.assertEqual(scrubbed, {})
        self.assertEqual(removed, ["CODEX_API_KEY", "GH_TOKEN"])

    def test_other_codex_prefixed_secrets_still_removed(self):
        env = {"CODEX_HOME": r"C:\fake", "CODEX_TOKEN": FAKE_TOKEN,
               "CODEX_SOMETHING_SECRET": FAKE_TOKEN}
        scrubbed, removed = env_guard.scrub_secrets(env, keep_paths=env_guard.PATH_ENV_NAMES)
        self.assertIn("CODEX_HOME", scrubbed)
        self.assertEqual(sorted(removed), ["CODEX_SOMETHING_SECRET", "CODEX_TOKEN"])

    def test_codex_home_is_not_reported_as_secret(self):
        self.assertFalse(env_guard.is_secret_name("CODEX_HOME"))
        self.assertTrue(env_guard.is_secret_name("CODEX_API_KEY"))
        self.assertTrue(env_guard.should_strip("CODEX_HOME"))
        self.assertFalse(env_guard.should_strip("CODEX_HOME", env_guard.PATH_ENV_NAMES))

    def test_user_path_variables_survive_scrub(self):
        names = ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA")
        scrubbed, removed = env_guard.scrub_secrets({name: r"C:\fake" for name in names})
        for name in names:
            self.assertIn(name, scrubbed)
        self.assertEqual(removed, [])


class CodexEnvIsolationTests(unittest.TestCase):
    """The builder/explorer/reviewer/fixer subprocess must never see release tokens."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cred-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_codex_child_env_has_no_tokens(self):
        dump = self.tmp / "env.json"
        env = {"GH_TOKEN": FAKE_TOKEN, "GITHUB_TOKEN": FAKE_TOKEN,
               "MY_SECRET_KEY": FAKE_TOKEN, "FAKE_ENV_DUMP": str(dump),
               "PATH": os.environ["PATH"], "FAKE_CODEX_MODE": "nochange"}
        run = codex.run_codex("ping", self.repo, sandbox="read-only", timeout_s=60,
                              codex_cmd=[sys.executable, FAKE_CODEX], extra_env=env)
        self.assertTrue(run.ok, run.detail)
        keys = set(json.loads(dump.read_text(encoding="utf-8")))
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "MY_SECRET_KEY"):
            self.assertNotIn(name, keys)
        self.assertIn("PATH", keys)

    def test_test_executor_env_scrubbed(self):
        env = support.sanitized_env()
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("GITHUB_TOKEN", env)

    def test_test_executor_env_has_no_codex_home(self):
        """CODEX_HOME 只给 Codex 认证环境；普通测试执行器不该拿到。"""
        with mock.patch.dict(os.environ, {"CODEX_HOME": r"C:\fake\codex-home"}):
            env = support.sanitized_env()
            self.assertNotIn("CODEX_HOME", env)
            self.assertIn("CODEX_HOME", codex.child_env())

    def test_codex_child_env_keeps_codex_home_but_not_tokens(self):
        dump = self.tmp / "env_home.json"
        env = {"CODEX_HOME": r"C:\fake\codex-home", "GH_TOKEN": FAKE_TOKEN,
               "GITHUB_TOKEN": FAKE_TOKEN, "FAKE_ENV_DUMP": str(dump),
               "PATH": os.environ["PATH"], "FAKE_CODEX_MODE": "nochange"}
        run = codex.run_codex("ping", self.repo, sandbox="read-only", timeout_s=60,
                              codex_cmd=[sys.executable, FAKE_CODEX], extra_env=env)
        self.assertTrue(run.ok, run.detail)
        keys = set(json.loads(dump.read_text(encoding="utf-8")))
        self.assertIn("CODEX_HOME", keys)
        for name in ("GH_TOKEN", "GITHUB_TOKEN"):
            self.assertNotIn(name, keys)

    def test_login_state_uses_same_scrubbed_env(self):
        captured = {}

        class _FakeProc:
            returncode = 0
            stdout = "Logged in using ChatGPT"
            stderr = ""

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            return _FakeProc()

        with mock.patch.dict(os.environ, {"CODEX_HOME": r"C:\fake\codex-home",
                                          "GH_TOKEN": FAKE_TOKEN}):
            with mock.patch.object(codex.subprocess, "run", side_effect=_fake_run):
                ok, message = codex.login_state([sys.executable, FAKE_CODEX])

        self.assertTrue(ok, message)
        self.assertEqual(captured["cmd"][-2:], ["login", "status"])
        self.assertIn("CODEX_HOME", captured["env"])
        self.assertNotIn("GH_TOKEN", captured["env"])

    def test_login_state_reports_not_logged_in(self):
        class _FakeProc:
            returncode = 1
            stdout = ""
            stderr = "Not logged in"

        with mock.patch.object(codex.subprocess, "run", side_effect=lambda *a, **k: _FakeProc()):
            ok, message = codex.login_state([sys.executable, FAKE_CODEX])
        self.assertFalse(ok)
        self.assertIn("not logged in", message.lower())

    def test_publisher_still_sees_release_token(self):
        cfg = {"github": {"enabled": True, "require_env_token": True, "token_env": "GH_TOKEN"}}
        publisher = gp.Publisher(self.tmp, cfg, gh_cmd=[sys.executable, "missing"])
        with mock.patch.dict(os.environ, {"GH_TOKEN": FAKE_TOKEN}):
            self.assertIsNone(publisher._token_missing())
            args = publisher._credential_args()
        self.assertNotIn(FAKE_TOKEN, " ".join(args))
        self.assertIn("GH_TOKEN", " ".join(args))


class GitCredentialNegativeTests(unittest.TestCase):
    """Prove no silent fallback to the cached account credential when a fake token is set."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cred-git-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin",
                        "https://github.com/zwmhua2-ai-bot/soloship-demo.git"], check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_git_with_fake_token_cannot_read_private_repo(self):
        proc = subprocess.run(
            ["git", "-c", "credential.helper=", "-c",
             'credential.helper=!f() { echo username=x-access-token; echo "password=$GH_TOKEN"; }; f',
             "ls-remote", "origin"],
            cwd=str(self.repo), capture_output=True, text=True, timeout=60,
            env={"GH_TOKEN": FAKE_TOKEN, "PATH": os.environ["PATH"],
                 "GIT_TERMINAL_PROMPT": "0"},
            stdin=subprocess.DEVNULL,
        )
        self.assertNotEqual(proc.returncode, 0,
                            "假 Token 不应能访问私有仓库（说明存在缓存凭据回退）")
        self.assertNotIn(FAKE_TOKEN, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
