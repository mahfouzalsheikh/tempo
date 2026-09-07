"""Exercise the deployed build recipe without creating runs or calling a model."""

import asyncio
import io
import json
import os
import tempfile
import zipfile
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tempo_web.settings")
django.setup()

from tempo.build_artifacts import package_directory, verify_artifact  # noqa: E402
from tempo.build_profiles import MINI_APP, prepare_build  # noqa: E402
from tempo.config import HooksConfig, ValidationConfig  # noqa: E402
from tempo.validation import ProjectValidator  # noqa: E402
from tempo.workspace import WorkspaceManager  # noqa: E402
from tempo_web.models import BuildArtifact  # noqa: E402


async def main():
    root = Path(os.environ["TEMPO_WORKSPACE_ROOT"])
    with tempfile.TemporaryDirectory(prefix="static-build-smoke-", dir=root) as directory:
        workspace = Path(directory)
        app = workspace / "mini-app"
        app.mkdir()
        package = {
            "name": "tempo-build-smoke",
            "version": "1.0.0",
            "scripts": {
                "prepare:assets": "node prepare.cjs",
                "test": "node prepare.cjs",
                "build": "node build.cjs",
                "validate:dist": "node check.cjs",
            },
        }
        (app / "package.json").write_text(json.dumps(package))
        (app / "package-lock.json").write_text(
            json.dumps(
                {
                    "name": package["name"],
                    "version": "1.0.0",
                    "lockfileVersion": 3,
                    "requires": True,
                    "packages": {"": {"name": package["name"], "version": "1.0.0"}},
                }
            )
        )
        (app / "prepare.cjs").write_text(
            "for(const k of ['GITHUB_TOKEN','OPENAI_API_KEY','DOCKER_HOST',"
            "'TEMPO_VALIDATION_RUNNER_TOKEN'])if(process.env[k])throw Error('Unexpected grant');"
        )
        (app / "build.cjs").write_text(
            "const fs=require('fs');fs.mkdirSync('dist');"
            "fs.writeFileSync('dist/index.html','<h1>Verified build</h1>');"
        )
        (app / "check.cjs").write_text(
            "if(require('fs').readFileSync('dist/index.html','utf8')"
            "!=='<h1>Verified build</h1>')throw Error('Wrong build output');"
        )
        events = []

        async def on_event(event):
            events.append(event)

        await prepare_build(MINI_APP, workspace, on_event)
        bundle = None

        async def capture():
            nonlocal bundle
            bundle = await asyncio.to_thread(package_directory, workspace, MINI_APP["output"])

        validator = ProjectValidator(
            ValidationConfig(
                required_checks=MINI_APP["checks"], cleanup_command="rm -rf mini-app/dist"
            ),
            WorkspaceManager(root, HooksConfig()),
            on_event,
            set(),
            after_checks=capture,
        )
        result = await validator.execute({"summary": "Static build deployment probe"}, workspace)
        assert result["success"], "Deployed static build checks failed"
        assert not (app / "dist").exists(), "Cleanup did not finish"
        with zipfile.ZipFile(io.BytesIO(bundle["data"])) as archive:
            assert archive.read("index.html") == b"<h1>Verified build</h1>"
    print("Build preparation grants, offline checks, archive capture, and cleanup passed.")


asyncio.run(main())
count = 0
for artifact in BuildArtifact.objects.iterator(chunk_size=1):
    verify_artifact(artifact)
    count += 1
print(f"{count} retained build artifacts verified. No production work created or dispatched.")
