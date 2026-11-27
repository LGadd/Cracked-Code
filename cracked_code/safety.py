"""Workspace boundary, command deny-list, approvals, and the per-turn git snapshot (kept outside the workspace)."""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from . import config, ui


SNAPSHOT = True  # config `snapshot`
MAX_MB = config.DEFAULTS["snapshot_max_mb"]  # config `snapshot_max_mb`: bigger files stay out of the snapshots; 0 = no limit
SKIP: list[str] = list(config.DEFAULTS["snapshot_skip"])  # config `snapshot_skip`: gitignore patterns kept out

# Refused outright. This list is deliberately short: the harness has no opinion
# about what the model is allowed to work on, it only blocks the unrecoverable.
DENY: list[tuple[str, str]] = [
    (r"\brm\b[^|;&]*\s/(?:\s|$|\*)", "rm of the filesystem root"),
    (r"\brm\b[^|;&]*\s~(?:/\*)?(\s|$)", "rm of the home directory"),
    (r"\bmkfs(?:\.\w+)?\b", "mkfs rewrites a filesystem"),
    (r"\bdd\b[^|;&]*\bof=/dev/", "dd writing to a block device"),
    (r">\s*/dev/(sd|nvme|hd)", "redirect onto a block device"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "changes the power state"),
    (r"\bchmod\b[^|;&]*-R[^|;&]*\s/(?:\s|$)", "recursive chmod of /"),
    (r"\bfind\b[^|;&]*-delete\b", "find -delete"),
    (r":\(\)\s*\{[^}]*\}\s*;:", "fork bomb"),
]
_COMPILED = [(re.compile(p), why) for p, why in DENY]


class Guard:
    def __init__(self, workspace: Path, assume_yes: bool = False, out=sys.stderr):
        self.workspace = workspace
        self.assume_yes = assume_yes
        self.investigate = False  # INVESTIGATE mode: read only, tasks get agent.INVESTIGATE (Shift+Tab cycles modes)
        self.explain = None  # approval menu e: why -> the model's reason (the REPL sets it; None hides the option)
        self.out = out
        self.always: set[str] = set()

    def resolve(self, raw: str) -> Path:
        p = Path(os.path.expanduser(str(raw)))
        if not p.is_absolute():
            p = self.workspace / p
        # shortcut: symlinks are not resolved, so a symlink pointing outside the
        # workspace escapes the boundary. Upgrade: Path.resolve(strict=True) if
        # the model starts using symlinks as an escape hatch.
        return Path(os.path.normpath(str(p)))

    def outside(self, p: Path) -> bool:
        w = str(self.workspace)
        return str(p) != w and not str(p).startswith(w + os.sep)

    def allow(self, key: str, why: str, always_ask: bool = False, preview: str = "", even_fuck_it: bool = False) -> bool:
        """always_ask (deletes): ignore Fuck It Mode and "always" (no a option); only y, this once.
        even_fuck_it (writes outside the workspace): ask in Fuck It Mode too, but "a" still sticks for this key.
        preview: a unified diff shown above the question, so nothing is approved blind."""
        if not always_ask and (key in self.always or (self.assume_yes and not even_fuck_it)):
            return True
        if preview and ui.ON:  # not a plain pipe: indented lines read as tool results there
            ui.out(ui.diff(preview))
        ui.bell()
        options = [("y", "yes"), *([] if always_ask else [("a", "always: don't ask again for this")]),
                   *([("e", "explain: the model says why it wants this")] if self.explain else []), ("n", "no")]
        while True:
            # Enter alone and the first highlight are "no": nothing is approved by accident
            answer = ui.choose(why, options, tag="ALLOW?", default="n", detail=preview, mode=not always_ask)
            if answer != "e":
                break
            options = [o for o in options if o[0] != "e"]  # asked once; the same question gets the same answer
            ui.out(ui.paint(". asking the model why…", ui.MUTED))
            try:
                reason = self.explain(why)
            except (Exception, KeyboardInterrupt) as e:
                reason = f"(no answer: {e or 'stopped'})"
            ui.out(ui.paint("why: ", ui.NEON, bold=True) + reason)
        if answer == "/mode":  # Shift+Tab at an approval: yes, and stop asking
            self.assume_yes, self.investigate = True, False
            self.out.write(ui.paint("Fuck It Mode on: everything is auto-approved\n", ui.CRIT, bold=True))
            return True
        if answer == "a":
            self.always.add(key)
            return True
        return answer == "y"

    def writes_outside(self, command: str) -> str | None:
        """Why a shell command needs asking even in Fuck It Mode: it uses sudo, or writes a path outside the workspace
        (/tmp and /dev/null aside). Made after a model, stuck on a check, put 14 dummy commands in ~/.local/bin (2026-11-11).
        shortcut: a pattern match on the command line; `cd /x && touch y`, `sed -i`, `cp -t dir` and scripts that
        write files get past it. Upgrade to a sandbox (bwrap) if the model starts writing outside on purpose."""
        if re.search(r"(?:^|[\s;&|(])sudo\b", command):
            return "uses sudo"
        paths = [t for t in re.findall(r"\d*>>?\|?\s*([^\s;&|<>()]+)", command) if not t.startswith("&")]
        paths += re.findall(r"\bof=([^\s;&|]+)", command)
        for segment in re.split(r"&&|\|\||[;|\n()]", command):
            try:
                words = shlex.split(segment, comments=True)
            except ValueError:
                words = segment.split()
            while words and ("=" in words[0] or words[0] in ("env", "nohup", "time", "command")):
                words = words[1:]
            if not words:
                continue
            cmd, args = os.path.basename(words[0]), [a for a in words[1:] if not a.startswith("-")]
            if cmd in ("chmod", "chown", "chgrp"):
                args = args[1:]  # the mode or owner
            if cmd in ("mkdir", "touch", "chmod", "chown", "chgrp", "rm", "rmdir", "tee", "truncate", "unlink", "shred"):
                paths += args
            elif cmd in ("cp", "mv", "ln", "install"):
                paths += args[-1:]
        home, tmp = os.path.expanduser("~"), tempfile.gettempdir()
        for raw in paths:
            p = Path(os.path.normpath(os.path.expanduser(re.sub(r"\$\{?HOME\}?", lambda _: home, raw.strip("'\"")))))
            posix_abs = raw.strip("'\"").startswith(("/", "~", "$HOME", "${HOME}"))  # Git Bash: /etc is absolute there
            if not (p.is_absolute() or posix_abs) or not self.outside(p):
                continue  # relative paths start in the workspace (cd is not followed)
            q = str(p).replace(os.sep, "/")  # Windows: \\tmp\\x from a Git Bash /tmp/x
            if q.startswith(("/tmp/", tmp.replace(os.sep, "/") + "/", "/dev/null", "/dev/std", "/dev/fd/")) or q in ("/tmp", tmp.replace(os.sep, "/")):
                continue
            return f"writes outside the workspace: {p}"
        return None

    def shell_denied(self, command: str) -> str | None:
        for pattern, why in _COMPILED:
            if pattern.search(command):
                return f"{command!r} is blocked: {why}"
        return None


def home_warning(workspace: Path) -> str | None:
    """Warn (startup asks [y/N]) when the workspace is the home folder or /.

    SSH logins start in ~, so a bare `cracked` there hung for minutes staging 38 GB (2026-11-03).
    """
    ws = workspace.resolve()
    if ws == Path.home().resolve():
        return f"{ws} is your home folder, not a project: every turn will snapshot it."
    if ws == Path(ws.anchor):
        return f"{ws} is the filesystem root, not a project: every turn will snapshot it."
    return None


def _files(workspace: Path):
    """(path relative to the workspace, bytes) of every file, skipping .git, .cracked and the snapshot
    store (inside the workspace when that is ~); symlinks not followed."""
    own = str(store(workspace))
    # plain folder patterns in snapshot_skip (node_modules/, .venv/) are never staged, so not walked either
    skip = {".git", ".cracked"} | {p[:-1] for p in SKIP if p.endswith("/") and not any(c in p[:-1] for c in "/*?[\\")}
    for root, dirs, names in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in skip and os.path.join(root, d) != own]
        for n in names:
            p = os.path.join(root, n)
            with contextlib.suppress(OSError):
                yield os.path.relpath(p, workspace), os.lstat(p).st_size


def _too_big(size: int) -> bool:
    return 0 < MAX_MB * 1024 * 1024 < size


def workspace_size(workspace: Path) -> tuple[int, int]:
    """(bytes, files) the snapshot will cover: files over snapshot_max_mb or matching snapshot_skip are left out.

    Ignores .gitignore, so it can overstate what the snapshot will stage.
    """
    names = [p for p in SKIP if "/" not in p]  # file patterns (*.png); folder ones are not walked at all
    sizes = [s for rel, s in _files(workspace)
             if not _too_big(s) and not any(fnmatch.fnmatch(os.path.basename(rel), p) for p in names)]
    return sum(sizes), len(sizes)


def store(workspace: Path) -> Path:
    """The snapshot repo for `workspace`. It lives outside it, so the user's own git history stays clean."""
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "cracked" / "snapshots" / hashlib.sha1(str(workspace.resolve()).encode()).hexdigest()[:16]


def _exclude(workspace: Path) -> None:
    """Keep snapshot_skip patterns and files over snapshot_max_mb out: listed in the store's exclude,
    and untracked if a snapshot already holds them (a video that was small mid-render, a new pattern).

    Done before `git add`, which would otherwise read every byte of a 20 GB model to hash it."""
    big = [rel for rel, s in _files(workspace) if _too_big(s)]
    # gitignore syntax: escape the glob characters so each line matches exactly one path
    lines = ["/" + "".join("\\" + c if c in "\\*?[!# " else c for c in rel) for rel in big if "\n" not in rel]
    (store(workspace) / "info").mkdir(exist_ok=True)
    own = [f"/{store(workspace).relative_to(workspace.resolve()).as_posix()}/"] if store(workspace).is_relative_to(workspace.resolve()) else []
    (store(workspace) / "info" / "exclude").write_text("\n".join(SKIP + own + lines) + "\n", encoding="utf-8")
    if held := git(workspace, "ls-files", "-z", "-ci", "--exclude-standard").stdout:
        git(workspace, "--literal-pathspecs", "rm", "--cached", "-q", "--ignore-unmatch",
            "--pathspec-from-file=-", "--pathspec-file-nul", input=held)


def snapshot(workspace: Path, note: str) -> tuple[str | None, str | None]:
    """Commit the whole workspace so any damage is one `git checkout .` away.

    Returns (sha, None) after a commit, (None, None) when there was nothing to
    commit, (None, reason) when the net has a hole.
    """

    def why(r: subprocess.CompletedProcess) -> str:
        # git puts some fatal reasons on stdout only, so check both streams.
        return " ".join((r.stderr or r.stdout).split())

    if not SNAPSHOT:
        return None, None
    repo = store(workspace)
    if not repo.exists():
        init = subprocess.run(["git", "init", "-q", "--bare", str(repo)], capture_output=True, text=True)
        if init.returncode:
            return None, why(init)
        (repo / "workspace").write_text(f"{workspace.resolve()}\n", encoding="utf-8")  # which folder this store belongs to
    _exclude(workspace)
    # --ignore-errors: a nested repo with no commit checked out (a scratch dir
    # left behind mid-run) makes git add exit non-zero; everything else stages.
    add = git(workspace, "add", "-A", "--ignore-errors")
    if not git(workspace, "diff", "--cached", "--name-only").stdout.strip():
        # A repeated warning beats a silent hole in the safety net.
        return None, (why(add) or "git add failed") if add.returncode else None
    commit = git(
        workspace, "-c", "user.name=harness", "-c", "user.email=harness@localhost",
        "commit", "-q", "-m", note,
    )
    if commit.returncode:
        return None, why(commit) or why(add)
    return git(workspace, "rev-parse", "--short", "HEAD").stdout.strip(), None


def git(workspace: Path, *args: str, **kw) -> subprocess.CompletedProcess:
    """git on the snapshot store, with the workspace as its working tree."""
    return subprocess.run(["git", "-C", str(workspace), f"--git-dir={store(workspace)}", f"--work-tree={workspace}", *args],
                          capture_output=True, text=True, **kw)


def head(workspace: Path) -> str | None:
    """The full sha of the last snapshot, or None when there is none yet (or snapshots are off)."""
    if not SNAPSHOT:
        return None
    r = git(workspace, "rev-parse", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else None


def restore(workspace: Path, sha: str) -> str:
    """/undo: put the workspace's files back as they were at `sha`.

    The current state is snapshotted first and the restore is a new commit, so nothing is
    lost: `git log` still has the undone work. Gitignored files and anything outside the
    workspace (or done by a command, like an installed package) are not touched.
    """
    snapshot(workspace, "harness: before undo")
    added = git(workspace, "diff", "--name-only", "--diff-filter=A", sha, "HEAD").stdout.split("\n")
    r = git(workspace, "checkout", sha, "--", ".")
    if r.returncode:
        return "ERROR: " + " ".join((r.stderr or r.stdout).split())
    for rel in filter(None, added):  # files created since sha: checkout leaves them, undo removes them
        with contextlib.suppress(OSError):
            p = workspace / rel
            p.unlink()
            for parent in p.parents:  # and folders the task made for them, once empty
                if parent == workspace or any(parent.iterdir()):
                    break
                parent.rmdir()
    sha_new, err = snapshot(workspace, f"harness: undo to {sha[:7]}")
    if err:
        return "ERROR: " + err
    if not sha_new:
        return "nothing to undo: the files were already as they were"
    return git(workspace, "diff", "--shortstat", "HEAD~1", "HEAD").stdout.strip() or "restored"


def diff(workspace: Path, since: str) -> tuple[str, str]:
    """(stat, patch) of everything changed since `since`, snapshotting the present first."""
    snapshot(workspace, "harness: /diff")
    stat = git(workspace, "diff", "--stat", since, "HEAD").stdout.rstrip()
    patch = git(workspace, "diff", since, "HEAD").stdout
    return stat, patch
