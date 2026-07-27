"""presence.session_pid — record the long-lived SESSION pid, not the short-lived inject-hook
subprocess pid (2026-07-15 bugfix; coordinator report + parked presence stand-down gap).

The hook process's pid dies the moment the hook returns, so recording it makes every liveness /
occupancy check see a dead pid within a second. session_pid() walks up the parent chain to the
agent-session process and records THAT pid instead.
"""
import unittest

from pm_mesh import presence


class SessionPidWalkTest(unittest.TestCase):
    def _reader(self, tree):
        return lambda pid: tree.get(pid)

    def test_walks_up_to_claude_comm_skipping_home_claude_shell(self):
        # The DECOY is in the walk path: an intermediate bash whose cmdline contains "/home/alice"
        # (the shell-snapshot bash) must NOT be picked — comm is 'bash', and its path is not a
        # strong binary token. The real session is the ancestor whose comm is exactly 'claude'.
        tree = {
            100: (90, "python3", "python3 -m pm_mesh.inject"),
            90:  (80, "bash", "/bin/bash -c source /home/alice/.claude/shell-snapshots/snapshot-bash"),
            80:  (70, "claude", "/home/alice/.vscode-server/extensions/anthropic.claude-code-2.1.209/x"),
            70:  (1,  "node", "vscode-server-main"),
        }
        self.assertEqual(presence.session_pid(100, read_proc=self._reader(tree)), 80)

    def test_matches_generic_interpreter_by_strong_cmdline_token(self):
        # codex/gemini run under comm 'node'; a strong binary token identifies them.
        tree = {10: (9, "python3", "hook"), 9: (1, "node", "node /opt/codex-cli/index.js exec")}
        self.assertEqual(presence.session_pid(10, read_proc=self._reader(tree)), 9)

    def test_falls_back_to_start_when_no_agent_ancestor(self):
        tree = {5: (4, "python3", "hook"), 4: (3, "bash", "bash -c cd /home/alice/x"), 3: (1, "sshd", "sshd")}
        self.assertEqual(presence.session_pid(5, read_proc=self._reader(tree)), 5)

    def test_fallback_when_proc_unreadable(self):
        self.assertEqual(presence.session_pid(42, read_proc=lambda pid: None), 42)

    def test_bounded_and_cycle_safe(self):
        self.assertEqual(presence.session_pid(2, read_proc=self._reader({2: (2, "x", "x")})), 2)

    def test_start_itself_is_the_agent(self):
        # If the caller IS the session process (heartbeat called directly, not via a hook), return it.
        tree = {77: (1, "claude", "claude")}
        self.assertEqual(presence.session_pid(77, read_proc=self._reader(tree)), 77)


class ReadProcParseTest(unittest.TestCase):
    def test_read_proc_handles_comm_with_spaces_and_parens(self):
        # /proc/<pid>/stat comm can contain spaces and ')'; the real /proc is read for os.getpid().
        info = presence._read_proc(__import__("os").getpid())
        self.assertIsNotNone(info)
        ppid, comm, cmdline = info
        self.assertIsInstance(ppid, int)
        self.assertIsInstance(comm, str)

    def test_read_proc_none_for_bogus_pid(self):
        self.assertIsNone(presence._read_proc(2 ** 31 - 1))


if __name__ == "__main__":
    unittest.main()
