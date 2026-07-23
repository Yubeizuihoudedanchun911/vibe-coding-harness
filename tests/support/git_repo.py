from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


class GitRepositoryFixture:
    target: Path

    def setUpGitRepository(self) -> None:
        self.git_temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.git_temporary.cleanup)
        self.target = Path(self.git_temporary.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Vibe Tests")
        self.git("config", "user.email", "vibe-tests@example.invalid")
        (self.target / "README.md").write_text("base\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-m", "base")

    def git(
        self,
        *arguments: str,
        cwd: Path | None = None,
        input_text: str | None = None,
    ) -> str:
        result = subprocess.run(
            ["git", "-C", str(cwd or self.target), *arguments],
            check=False,
            capture_output=True,
            text=True,
            input=input_text,
        )
        if result.returncode != 0:
            self.fail(
                f"git {' '.join(arguments)} failed\n"
                f"stdout={result.stdout}\nstderr={result.stderr}"
            )
        return result.stdout.strip()
