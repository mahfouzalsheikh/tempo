"""Run inside the deployed acceptance worker; no production records or model calls."""

import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path

from tempo.acceptance_contract import digest, specification, verify_report
from tempo.acceptance_runner import execute
from tempo.build_artifacts import package_directory
from tempo.validation_auth import runner_environment


async def docker(*arguments):
    process = await asyncio.create_subprocess_exec(
        "docker",
        *arguments,
        env=runner_environment(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, error = await asyncio.wait_for(process.communicate(), 5)
    if process.returncode:
        raise RuntimeError(error.decode(errors="replace")[-500:])
    return output


async def exercise(image, directory, inspect):
    key = uuid.uuid4()
    task = asyncio.create_task(execute(image, directory, key))
    try:
        if inspect:
            for _ in range(50):
                try:
                    container = json.loads(await docker("inspect", f"tempo-acceptance-{key.hex}"))[
                        0
                    ]
                    if container["State"]["Running"]:
                        break
                except RuntimeError:
                    pass
                await asyncio.sleep(0.1)
            else:
                raise AssertionError("Acceptance container did not start")
            assert container["Image"] == image
            assert container["HostConfig"]["NetworkMode"] == "none"
            assert container["HostConfig"]["ReadonlyRootfs"]
            assert len(container["Mounts"]) == 1
            assert container["Mounts"][0]["Destination"] == "/input"
            assert not container["Mounts"][0]["RW"]
            await docker(
                "exec",
                "--user",
                "10001:10001",
                f"tempo-acceptance-{key.hex}",
                "python",
                "-c",
                """
import os, socket
from pathlib import Path
assert os.getuid() == 10001
assert not {'GITHUB_TOKEN', 'OPENAI_API_KEY', 'DOCKER_HOST', 'DATABASE_URL'} & os.environ.keys()
assert not Path('/data/workspaces').exists() and not Path('/home/tempo/.codex').exists()
try:
    Path('/input/write-probe').write_text('unexpected')
except OSError:
    pass
else:
    raise AssertionError('Acceptance inputs are writable')
for host in ('1.1.1.1', '169.254.169.254', '172.17.0.1'):
    try:
        connection = socket.create_connection((host, 443), timeout=0.3)
    except OSError:
        continue
    connection.close()
    raise AssertionError('Acceptance container has outbound network access')
""",
            )
        return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


root = Path(os.environ["TEMPO_WORKSPACE_ROOT"])
image = os.environ["TEMPO_ACCEPTANCE_IMAGE"]
with tempfile.TemporaryDirectory(prefix="acceptance-probe-", dir=root) as temporary:
    directory = Path(temporary)
    output = directory / "dist"
    output.mkdir()
    (output / "index.html").write_text("""
<label>Item name<input id="name"></label>
<button onclick="saveItem()">
Add item</button><p data-testid="result" id="result">Empty</p>
<p id="network">Checking network</p>
<label>Photo<input type="file" id="photo"
onchange="document.getElementById('upload').textContent=this.files[0].name"></label>
<p id="upload">No photo</p>
<label><input type="radio"
onchange="document.getElementById('style').textContent='Style chosen'">Example style</label>
<p id="style">Unset</p>
<label>Detail<input type="range" min="0" max="10" value="5"
oninput="document.getElementById('detail').textContent='Detail '+this.value"></label>
<p id="detail">Detail 5</p>
<button onclick="saveSvg(false)">Download SVG</button>
<button onclick="saveSvg(true)">Download invalid SVG</button>
<script>
function saveSvg(invalid){
const svg=invalid?'not SVG':
'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96">' +
'<circle cx="48" cy="48" r="30"/></svg>';
const a=document.createElement('a');
a.href=URL.createObjectURL(new Blob([svg], {type:'image/svg+xml'}));
a.download='drawing.svg';a.click();
}
function saveItem(){
result.textContent=document.getElementById('name').value;
localStorage.setItem('saved', result.textContent);
}
result.textContent=localStorage.getItem('saved') || 'Empty';
Promise.all(['http://1.1.1.1/', 'http://169.254.169.254/', 'http://127.0.0.1:2375/']
.map(url=>fetch(url).then(()=>false,()=>true))).then(results=>{
setTimeout(()=>{
document.getElementById('network').textContent=
results.every(Boolean)?'Network blocked':'Network open';
}, 1500);
});
</script>
""")
    bundle = package_directory(directory, "dist")
    criteria = [{"id": "AC-1"}, {"id": "AC-2"}]
    instructions = {
        "AC-1": 'open /\nfill "Item name" "Example item"\nclick button "Add item"\n'
        'expect testid "result" "Example item"',
        "AC-2": 'expect text "Network blocked"\nexpect testid "result" "Empty"',
    }
    (directory / "artifact.zip").write_bytes(bundle["data"])
    for version, passing in ((1, True), (1, False), (2, True), (2, False)):
        if not passing:
            instructions["AC-1"] = 'expect text "Deliberately missing acceptance text"'
        if version == 2:
            name = "Download SVG" if passing else "Download invalid SVG"
            instructions["AC-1"] = (
                'click text "Example style"\nexpect text "Style chosen"\n'
                'press slider "Detail" Home\nexpect text "Detail 0"\n'
                'upload label "Photo" png-circle-v1\nexpect text "circle.png"\n'
                f'download button "{name}" svg-v1'
            )
        suite = specification("a" * 64, criteria, instructions)
        (directory / "checks.json").write_text(
            json.dumps(
                {
                    "suite": suite,
                    "suite_digest": digest(suite),
                    "brief_digest": "a" * 64,
                    "criteria": criteria,
                    "artifact_digest": bundle["digest"],
                    "files": bundle["files"],
                }
            )
        )
        report = asyncio.run(exercise(image, directory, inspect=version == 1 and passing))
        assert verify_report(report, suite, bundle["digest"]) is passing
        assert report["results"][1]["status"] == "passed", "Browser state leaked between criteria"
print(
    "Offline browser interactions, fresh contexts, PNG uploads, SVG byte validation, "
    "and failing assertions verified for both runner versions."
)
