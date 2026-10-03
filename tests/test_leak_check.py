"""The leak guard: real customer information and API keys must never reach git, in any form. All data here is made up."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.db import Database

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "leak_check.py"
HOOKS = ROOT / "scripts" / "githooks"

KEY = "hcp-live-key-9f8e7d6c5b4a"
NAME = "Pat Q. Examplar"
STREET = "742 Evergreen Terrace"
PHONE = "480-555-0199"
CONTACT = "Morgan Contactperson"
CONTACT_PHONE = "(602) 555-0142"
HOME = "99 Staff Lane, Mesa, AZ 85201"
EMAIL = "dispatcher.lead@acme-hvac.test"
NOTE = "Customer said the gate code changes weekly"
SENSITIVE = [KEY, NAME, STREET, PHONE, CONTACT, CONTACT_PHONE, HOME, EMAIL, NOTE]


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in ("HCP_API_KEY", "MAPS_API_KEY", "LLM_API_KEY", "SESSION_SECRET", "WEBHOOK_SECRET",
                                                           "HCP_MODE", "DATABASE_PATH", "LEAK_CHECK_DBS")}
    env.update(extra)
    return env


class Sandbox:
    """A throwaway git repo, a database with real-looking rows, and helpers to run the guard in it."""

    def __init__(self, hooks=False, with_db=True):
        self.dir = Path(tempfile.mkdtemp())
        self.repo = self.dir / "repo"
        self.repo.mkdir()
        self.db_path = self.dir / "live.db"
        self.env = clean_env()
        self.sh("git", "init", "-q", "-b", "main")
        self.sh("git", "config", "user.email", "t@example.com")
        self.sh("git", "config", "user.name", "Tester")
        if hooks:
            self.sh("git", "config", "core.hooksPath", str(HOOKS))
        if with_db:
            self.make_db()

    def sh(self, *cmd, env=None, input=None):
        return subprocess.run(list(cmd), cwd=str(self.repo), capture_output=True, text=True, env=env or self.env, input=input)

    def make_db(self):
        Database(str(self.db_path))
        c = sqlite3.connect(self.db_path)
        c.execute("INSERT INTO jobs(hcp_job_id, customer_name, customer_phone, street) VALUES ('job_real_1', ?, ?, ?)", (NAME, PHONE, STREET))
        c.execute("INSERT INTO jobs(hcp_job_id, customer_name, customer_phone, street) VALUES ('job_demo_001', 'Dana Fakeman', '480-555-0333', '3150 S Example Ave')")
        c.execute("INSERT INTO warranty_details(hcp_job_id, data) VALUES ('job_real_1', ?)",
                  (json.dumps({"contact_name": CONTACT, "contact_phones": [CONTACT_PHONE], "street": "15 Maple Court"}),))
        c.execute("INSERT INTO technicians(hcp_employee_id, name, home_address) VALUES ('pro_1', 'Staff Person', ?)", (HOME,))
        c.execute("INSERT INTO technicians(hcp_employee_id, name, home_address) VALUES ('emp_demo_1', 'Alex R.', '1 Demo Way, Gilbert')")
        c.execute("INSERT INTO users(email, name, password_hash, role, created_at) VALUES (?, 'Lead', 'h', 'admin', 'x')", (EMAIL,))
        c.execute("INSERT INTO job_exceptions(hcp_job_id, reason, note, set_at) VALUES ('job_real_1', 'other', ?, 'x')", (NOTE,))
        c.commit()
        c.close()
        self.env["DATABASE_PATH"] = str(self.db_path)

    def write(self, name, content):
        p = self.repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode())

    def check(self, *args, stdin=None, env=None):
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=str(self.repo), capture_output=True, text=True, env=env or self.env, input=stdin)

    def commit(self, message="work", *flags):
        return self.sh("git", "commit", "-q", *flags, "-m", message)

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class FindingTests(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.s.write("README.md", "Nothing private here.\n")

    def tearDown(self):
        self.s.close()

    def tree(self):
        r = self.s.check("--tree")
        return r.returncode, r.stdout + r.stderr

    def test_a_clean_tree_passes_and_says_what_it_checked(self):
        rc, out = self.tree()
        self.assertEqual(rc, 0, out)
        self.assertIn("checked against 1 database(s), 1 real job(s)", out)

    def test_every_kind_of_customer_information_is_found(self):
        cases = [("customer name", f"The customer {NAME} called."), ("customer street address", f"Go to {STREET}."),
                 ("customer phone number", f"Call {PHONE} now."), ("warranty contact name", f"Ask for {CONTACT}."),
                 ("warranty contact phone number", f"Dial {CONTACT_PHONE}."), ("customer street address", "Visit 15 Maple Court."),
                 ("technician home address", f"Starts at {HOME}."), ("login email", f"Contact {EMAIL}."),
                 ("dispatcher note", f"Remember: {NOTE}.")]
        for label, line in cases:
            with self.subTest(label=label, line=line):
                self.s.write("notes.txt", f"harmless\n{line}\n")
                rc, out = self.tree()
                self.assertEqual(rc, 1, out)
                self.assertIn(f"{label} in notes.txt:2", out)
        self.s.write("notes.txt", "harmless\n")

    def test_it_never_prints_what_it_found(self):
        self.s.write("notes.txt", "\n".join(SENSITIVE))
        rc, out = self.tree()
        self.assertEqual(rc, 1)
        for secret in SENSITIVE:
            self.assertNotIn(secret, out)

    def test_names_match_whatever_the_case_and_spacing(self):
        for text in (NAME.upper(), NAME.lower(), "pat   q.\n examplar".replace("\n", " "), f"x{NAME}x"):
            self.s.write("notes.txt", text + "\n")
            self.assertEqual(self.tree()[0], 1, text)

    def test_phone_numbers_match_in_any_format(self):
        for text in ("(480) 555-0199", "480.555.0199", "4805550199", "480 555 0199", "+1 480 555 0199", "1-480-555-0199", "480-555-0199.",
                     "14805550199", "+14805550199", "1 (480) 555-0199", "tel:+1-480-555-0199"):
            self.s.write("notes.txt", f"call {text} today\n")
            self.assertEqual(self.tree()[0], 1, text)
        for text in ("480-555-0198", "4805550199123", "14805550199123"):                      # someone else's number, or part of a longer number
            self.s.write("notes.txt", f"call {text} today\n")
            self.assertEqual(self.tree()[0], 0, text)

    def test_demo_data_is_not_a_leak(self):
        self.s.write("fixtures.py", "Dana Fakeman 480-555-0333 3150 S Example Ave Alex R. 1 Demo Way, Gilbert\n")
        self.assertEqual(self.tree()[0], 0)

    def test_a_file_name_can_leak_too(self):
        self.s.write(f"jobs for {NAME}.txt", "x\n")
        rc, out = self.tree()
        self.assertEqual(rc, 1)
        self.assertIn("customer name in the file name", out)

    def test_binary_files_are_refused_because_a_screenshot_cannot_be_scanned(self):
        self.s.write("screenshot.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00 not scannable")
        rc, out = self.tree()
        self.assertEqual(rc, 1)
        self.assertIn("binary file (screenshot.png)", out)

    def test_ignored_files_are_not_part_of_what_would_be_committed(self):
        self.s.write(".gitignore", "data/\n")
        self.s.write("data/export.txt", f"{NAME} {PHONE}\n")
        self.assertEqual(self.tree()[0], 0)

    def test_the_api_key_is_found_from_the_environment_and_from_a_dotenv_file(self):
        self.s.write("notes.txt", f"key = {KEY}\n")
        env = dict(self.s.env, HCP_API_KEY=KEY)
        r = self.s.check("--tree", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("API key or secret (HCP_API_KEY) in notes.txt:1", r.stderr)
        self.assertNotIn(KEY, r.stderr + r.stdout)
        self.s.write("notes.txt", "fine\n")
        self.s.write(".env", f"HCP_API_KEY={KEY}\nDATABASE_PATH={self.s.db_path}\n")                # ignored by git in real use; here we check it is learnt
        self.s.write(".gitignore", ".env\n")
        self.s.write("other.txt", f"{KEY}\n")
        r = self.s.check("--tree", env=clean_env())
        self.assertEqual(r.returncode, 1, r.stderr)

    def test_a_short_value_is_not_treated_as_a_secret(self):
        self.s.write("notes.txt", "token abc\n")
        env = dict(self.s.env, HCP_API_KEY="abc")
        self.assertEqual(self.s.check("--tree", env=env).returncode, 0)


class DatabaseDiscoveryTests(unittest.TestCase):
    def test_it_fails_closed_in_a_live_session_with_no_database(self):
        s = Sandbox(with_db=False)
        try:
            s.write("a.txt", "x\n")
            for env in (dict(s.env, HCP_API_KEY=KEY), dict(s.env, HCP_MODE="live")):
                r = s.check("--tree", env=env)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn("REFUSING", r.stderr)
            self.assertEqual(s.check("--tree").returncode, 0)                                      # not a live session: nothing to protect
        finally:
            s.close()

    def test_the_database_can_be_named_four_ways(self):
        s = Sandbox(with_db=False)
        try:
            s.make_db()
            del s.env["DATABASE_PATH"]
            s.write("notes.txt", f"{NAME}\n")
            for how, args, env in (("DATABASE_PATH", [], dict(s.env, DATABASE_PATH=str(s.db_path))),
                                   ("LEAK_CHECK_DBS", [], dict(s.env, LEAK_CHECK_DBS=f"/nonexistent.db:{s.db_path}")),
                                   ("--db", ["--db", str(s.db_path)], s.env)):
                r = s.check("--tree", *args, env=env)
                self.assertEqual(r.returncode, 1, how)
            shutil.copy(s.db_path, s.repo / "local.db")                                            # .env with a path relative to the repo
            s.write(".env", "DATABASE_PATH=local.db\n")
            self.assertEqual(s.check("--tree", env=s.env).returncode, 1, ".env")
        finally:
            s.close()

    def test_the_database_is_left_exactly_as_it_was(self):
        s = Sandbox()
        try:
            s.write("a.txt", "x\n")
            before, files = s.db_path.read_bytes(), sorted(p.name for p in s.dir.iterdir())
            s.check("--tree")
            self.assertEqual(s.db_path.read_bytes(), before)
            new = set(p.name for p in s.dir.iterdir()) - set(files)                               # only SQLite's own side files may appear
            self.assertLessEqual(new, {"live.db-wal", "live.db-shm"})
        finally:
            s.close()

    def test_an_old_database_without_every_table_still_works(self):
        s = Sandbox(with_db=False)
        try:
            c = sqlite3.connect(s.dir / "old.db")
            c.execute("CREATE TABLE jobs (hcp_job_id TEXT, customer_name TEXT, customer_phone TEXT, street TEXT)")
            c.execute("INSERT INTO jobs VALUES ('job_real_9', ?, '', '')", (NAME,))
            c.commit()
            c.close()
            s.env["DATABASE_PATH"] = str(s.dir / "old.db")
            s.write("a.txt", NAME)
            self.assertEqual(s.check("--tree").returncode, 1)
        finally:
            s.close()


class StagedAndMessageTests(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()

    def tearDown(self):
        self.s.close()

    def test_staged_means_what_is_in_the_index_not_the_working_copy(self):
        self.s.write("a.txt", "clean\n")
        self.s.sh("git", "add", "a.txt")
        self.s.write("a.txt", f"clean\n{NAME}\n")                                                  # edited afterwards, not staged
        self.assertEqual(self.s.check("--staged").returncode, 0)
        self.s.sh("git", "add", "a.txt")
        r = self.s.check("--staged")
        self.assertEqual(r.returncode, 1)
        self.assertIn("customer name in a.txt:2", r.stderr)

    def test_a_staged_binary_file_is_refused(self):
        self.s.write("shot.png", b"\x00\x01\x02")
        self.s.sh("git", "add", "shot.png")
        self.assertEqual(self.s.check("--staged").returncode, 1)

    def test_a_commit_message_is_checked(self):
        msg = self.s.dir / "msg.txt"
        msg.write_text(f"Fix the parser for the {NAME} job\n")
        r = self.s.check("--message", str(msg))
        self.assertEqual(r.returncode, 1)
        self.assertIn("customer name in the commit message", r.stderr)
        msg.write_text("Fix the parser for a job with a long description\n")
        self.assertEqual(self.s.check("--message", str(msg)).returncode, 0)
        msg.write_text(f"key {KEY}\n")
        self.assertEqual(self.s.check("--message", str(msg), env=dict(self.s.env, HCP_API_KEY=KEY)).returncode, 1)


class HookTests(unittest.TestCase):
    """The real git hooks, in real repositories: a commit or push containing real data must not go through."""

    def setUp(self):
        self.s = Sandbox(hooks=True)
        self.remote = self.s.dir / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True)
        self.s.sh("git", "remote", "add", "origin", str(self.remote))
        self.s.write("README.md", "hello\n")
        self.s.sh("git", "add", "README.md")
        self.assertEqual(self.s.commit("first").returncode, 0)
        self.assertEqual(self.s.sh("git", "push", "-q", "-u", "origin", "main").returncode, 0)

    def tearDown(self):
        self.s.close()

    def commits(self, where="HEAD"):
        return self.s.sh("git", "rev-list", "--count", where).stdout.strip()

    def remote_commits(self):
        return subprocess.run(["git", "rev-list", "--count", "main"], cwd=str(self.remote), capture_output=True, text=True).stdout.strip()

    def test_a_clean_commit_and_push_go_through(self):
        self.s.write("a.txt", "fine\n")
        self.s.sh("git", "add", "a.txt")
        self.assertEqual(self.s.commit("add a").returncode, 0)
        self.assertEqual(self.s.sh("git", "push", "-q").returncode, 0)
        self.assertEqual(self.remote_commits(), "2")

    def test_a_commit_with_a_customer_in_a_file_is_blocked(self):
        self.s.write("a.txt", f"{NAME} lives at {STREET}\n")
        self.s.sh("git", "add", "a.txt")
        r = self.s.commit("add a")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("BLOCKED", r.stderr)
        self.assertEqual(self.commits(), "1")

    def test_a_commit_message_with_a_customer_is_blocked(self):
        self.s.write("a.txt", "fine\n")
        self.s.sh("git", "add", "a.txt")
        r = self.s.commit(f"Handle the {NAME} case")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.commits(), "1")

    def test_skipping_the_commit_hooks_still_cannot_get_it_pushed(self):
        self.s.write("a.txt", f"{PHONE}\n")
        self.s.sh("git", "add", "a.txt")
        self.assertEqual(self.s.commit("sneaky", "--no-verify").returncode, 0)                    # committed locally...
        r = self.s.sh("git", "push", "-q")
        self.assertNotEqual(r.returncode, 0)                                                       # ...but the push is refused
        self.assertIn("customer phone number in commit", r.stderr)
        self.assertEqual(self.remote_commits(), "1")

    def test_a_leak_only_in_a_commit_message_cannot_be_pushed_either(self):
        self.s.write("a.txt", "fine\n")
        self.s.sh("git", "add", "a.txt")
        self.assertEqual(self.s.commit(f"for {NAME}", "--no-verify").returncode, 0)
        self.assertNotEqual(self.s.sh("git", "push", "-q").returncode, 0)
        self.assertEqual(self.remote_commits(), "1")

    def test_a_leak_buried_in_an_earlier_commit_of_the_push_is_found(self):
        self.s.write("a.txt", f"{PHONE}\n")
        self.s.sh("git", "add", "a.txt")
        self.assertEqual(self.s.commit("leaky", "--no-verify").returncode, 0)
        self.s.write("b.txt", "clean\n")
        self.s.sh("git", "add", "b.txt")
        self.assertEqual(self.s.commit("clean one", "--no-verify").returncode, 0)                  # the newest commit is clean...
        self.s.write("c.txt", "clean too\n")
        self.s.sh("git", "add", "c.txt")
        self.assertEqual(self.s.commit("another", "--no-verify").returncode, 0)
        r = self.s.sh("git", "push", "-q")
        self.assertNotEqual(r.returncode, 0)                                                       # ...but the push as a whole is not
        self.assertEqual(self.remote_commits(), "1")

    def test_a_clean_file_with_a_customers_name_as_its_file_name_cannot_be_pushed(self):
        self.s.write(f"{NAME}.txt", "nothing inside\n")
        self.s.sh("git", "add", "--all")
        self.assertEqual(self.s.commit("named file", "--no-verify").returncode, 0)
        r = self.s.sh("git", "push", "-q")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("customer name in a file name in commit", r.stderr)
        self.assertEqual(self.remote_commits(), "1")

    def test_a_new_branch_is_scanned_too_and_a_binary_is_refused(self):
        self.s.sh("git", "checkout", "-q", "-b", "feature")
        self.s.write("shot.png", b"\x89PNG\x00\x00")
        self.s.sh("git", "add", "shot.png")
        self.assertEqual(self.s.commit("image", "--no-verify").returncode, 0)
        r = self.s.sh("git", "push", "-q", "-u", "origin", "feature")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("binary file", r.stderr)
        self.assertNotIn("feature", subprocess.run(["git", "branch"], cwd=str(self.remote), capture_output=True, text=True).stdout)

    def test_the_api_key_in_a_commit_is_blocked(self):
        env = dict(self.s.env, HCP_API_KEY=KEY)
        self.s.write("config.txt", f"HCP_API_KEY={KEY}\n")
        self.s.sh("git", "add", "config.txt")
        r = self.s.sh("git", "commit", "-q", "-m", "config", env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn(KEY, r.stderr + r.stdout)

    def test_in_a_live_session_with_no_database_nothing_can_be_committed(self):
        env = clean_env(HCP_API_KEY=KEY)                                                           # a key, but no database to check against
        self.s.write("a.txt", "fine\n")
        self.s.sh("git", "add", "a.txt")
        r = self.s.sh("git", "commit", "-q", "-m", "fine", env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("REFUSING", r.stderr)

    def test_deleting_a_branch_on_the_remote_is_not_blocked(self):
        self.s.sh("git", "checkout", "-q", "-b", "temp")
        self.assertEqual(self.s.sh("git", "push", "-q", "-u", "origin", "temp").returncode, 0)
        self.s.sh("git", "checkout", "-q", "main")
        self.assertEqual(self.s.sh("git", "push", "-q", "origin", "--delete", "temp").returncode, 0)


class ProjectSetupTests(unittest.TestCase):
    def test_the_hooks_exist_and_are_executable(self):
        for name in ("pre-commit", "commit-msg", "pre-push"):
            p = HOOKS / name
            self.assertTrue(p.is_file(), name)
            if os.name == "posix":
                self.assertTrue(os.access(p, os.X_OK), name)
            self.assertIn("leak_check.py", p.read_text())

    def test_the_installer_turns_the_guard_on(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "install_git_guard.py")], capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(r.returncode, 0, r.stderr)
        got = subprocess.run(["git", "config", "--get", "core.hooksPath"], capture_output=True, text=True, cwd=str(ROOT)).stdout.strip()
        self.assertEqual(got, "scripts/githooks")

    def test_this_repository_itself_is_clean(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--tree"], capture_output=True, text=True, cwd=str(ROOT), env=clean_env())
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_things_a_live_test_produces_are_ignored_by_git(self):
        for name in ("data/routing.db", "x/live.db", ".env", "live_check_report.json", "out/phase0_report.json", "a.sqlite"):
            r = subprocess.run(["git", "check-ignore", "-q", name], cwd=str(ROOT))
            self.assertEqual(r.returncode, 0, name)

    def test_the_instructions_for_new_sessions_state_the_rule(self):
        text = (ROOT / "CLAUDE.md").read_text()
        for needle in ("never reach GitHub", "install_git_guard.py", "--no-verify", "leak_check.py --tree", "DATABASE_PATH", "HCP_API_KEY"):
            self.assertIn(needle, text)
        self.assertEqual(subprocess.run([sys.executable, str(SCRIPT), "--tree"], cwd=str(ROOT), env=clean_env(), capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
