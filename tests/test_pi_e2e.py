# Opt in: LOOP_ENGINE_PI_E2E=1 python -m pytest -s tests/test_pi_e2e.py
# Optional paths: LOOP_ENGINE_PI_E2E_NATIVE_BIN, LOOP_ENGINE_PI_E2E_ADAPTER.
# Real Maven downloads; pi state = machine's existing login (auth.json symlinked, catalog/settings copied; contents never read). Temporary tree, NOT a sandbox.
import contextlib
import hashlib
import json
import os
from pathlib import Path
import selectors
import shlex
import shutil
import signal
import site
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1]
_ORIGINAL_RUN = subprocess.run
KEY = "e2e/tiny"
SPEC = "openspec/changes/e2e/specs/tiny/spec.md"
JAVA = "tiny/src/main/java/example/DoubleController.java"
STRONG_SPEC = """# Double controller
## Requirement: deterministic framework-free HTTP contract
Implement example.DoubleController.handle(String method, String path, String body).
It represents an HTTP controller without a server/framework, database or external dependency.
Method arguments represent the HTTP method, exact URL path (no query), and UTF-8 request body.
Response has public final int status and public final String body. All arguments are non-null.
HTTP contract: POST /double, Content-Type text/plain, body a signed decimal integer.
Respond with text/plain: canonical decimal n*2 on success. Do not add a network listener.
Validate path first, method second, body syntax third, then range. No trimming.

| Field | Type | Source | Constraint |
|---|---|---|---|
| method | String | HTTP method argument | case-sensitive; POST only; otherwise 405 |
| path | String | HTTP URL path argument | exactly /double; otherwise 404 |
| body | String | HTTP request body argument | regex -?[0-9]+; otherwise 400 INVALID |
| n | integer | parsed body | -100 <= n <= 100; any larger magnitude including overflow => 400 RANGE |
| status | int | response | 200, 400, 404 or 405 |
| response.body | String | response body | canonical n*2, INVALID, RANGE, NOT_FOUND or METHOD_NOT_ALLOWED |

### Scenario: positive
Given POST /double with body 7
When handle is invoked
Then status=200 and response.body=14.
### Scenario: zero
Given POST /double with body 0
When handle is invoked
Then status=200 and response.body=0.
### Scenario: negative
Given POST /double with body -7
When handle is invoked
Then status=200 and response.body=-14.
### Scenario: inclusive boundaries
Given POST /double with body 100 or -100
When handle is invoked
Then status=200 and response.body=200 or -200 respectively.
### Scenario: invalid syntax
Given POST /double with body empty, abc, +1 or a space
When handle is invoked
Then status=400 and response.body=INVALID.
### Scenario: range and overflow
Given POST /double with body 101, -101 or 999999999999999999999
When handle is invoked
Then status=400 and response.body=RANGE; no exception escapes.
### Scenario: method
Given GET /double with body 1
When handle is invoked
Then status=405 and response.body=METHOD_NOT_ALLOWED.
### Scenario: unknown path
Given GET /missing with body abc
When handle is invoked
Then status=404 and response.body=NOT_FOUND.
## External Dependencies
None. Use Java 8 and JUnit 4 only. No persistence, DTO library, DAO or service layer is required.
"""
STUB = """package example;
public class DoubleController {
    public static final class Response {
        public final int status;
        public final String body;
        public Response(int status, String body) { this.status = status; this.body = body; }
    }
    public Response handle(String method, String path, String body) {
        // TODO: implement the spec in the GREEN phase.
        return new Response(501, "TODO");
    }
}
"""


class Blocked(Exception):
    pass


def _write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _json(path, value):
    return _write(path, json.dumps(value, ensure_ascii=False, indent=2))


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


_OWNER_ENV = "_PI_E2E_PROCESS_OWNER"


def _process_table():
    out = _ORIGINAL_RUN(["ps", "-axo", "pid=,ppid=,pgid=,lstart="],
                        capture_output=True, text=True, timeout=2,
                        env=dict(os.environ, LC_ALL="C")).stdout
    rows = [line.split(maxsplit=3) for line in out.splitlines()]
    return {int(p): (int(parent), int(group), " ".join(born.split()))
            for p, parent, group, born in rows}


def _owned_processes(owner):
    # Local ownership discovery, NOT a sandbox. Stream and discard ps environment/argv;
    # never log them or retain anything except exact-match owned PID/birth pairs.
    env = dict(os.environ, LC_ALL="C")
    env.pop(_OWNER_ENV, None)  # The observer itself must not inherit an invocation's marker.
    owned = {}
    with subprocess.Popen(["ps", "eww", "-axo", "pid=,lstart=,command="],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True, errors="replace", env=env) as query:
        for line in query.stdout:
            fields = line.split(maxsplit=6)
            if len(fields) == 7 and f"{_OWNER_ENV}={owner}" in fields[6].split():
                owned[int(fields[0])] = " ".join(fields[1:6])
    if query.returncode:
        raise RuntimeError("local process ownership query failed")
    return owned


def _remember_children(pid, seen, table):
    if pid not in seen and pid in table:
        seen[pid] = table[pid][1:]
    changed = True
    while changed:
        changed = False
        for child, (parent, group, born) in table.items():
            if parent in seen and table.get(parent, (None, None, None))[2] == seen[parent][1]:
                if child not in seen:
                    seen[child] = (group, born)
                    changed = True


def _run_process(argv, *, input=None, timeout=600, ceiling=600, check=False,
                 capture_output=True, text=True, **kwargs):
    deadline = time.monotonic() + min(timeout, ceiling)
    seen = {}
    owner = uuid.uuid4().hex
    supplied_env = kwargs.pop("env", None)
    env = dict(os.environ if supplied_env is None else supplied_env)
    env.pop(_OWNER_ENV, None)  # Nested invocations own a new tree; ancestry is still tracked.
    env[_OWNER_ENV] = owner
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text,
                            start_new_session=True, env=env, **kwargs)

    def remember():
        owned = _owned_processes(owner)
        table = _process_table()
        for pid, born in owned.items():
            if table.get(pid, (None, None, None))[2] == born:
                seen[pid] = table[pid][1:]
        _remember_children(proc.pid, seen, table)
        return table

    try:
        while True:
            remember()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, min(timeout, ceiling))
            try:
                stdout, stderr = proc.communicate(input=input, timeout=min(0.2, remaining))
                result = subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
                if check:
                    result.check_returncode()
                return result
            except subprocess.TimeoutExpired:
                input = None
    finally:
        try:
            # Stop spawning before the final scans, but kill the parent only after children.
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.send_signal(signal.SIGSTOP)
            stopped = set()
            while True:
                table = remember()
                children = {pid: (group, born) for pid, (group, born) in seen.items()
                            if pid != proc.pid and table.get(pid, (None, None, None))[2] == born}
                pending = {(pid, born) for pid, (_, born) in children.items()} - stopped
                if not pending:
                    break
                for pid, _ in pending:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGSTOP)
                stopped.update(pending)
            # Never signal a group containing an unowned process or a reused group leader.
            groups = {pid for pid, (group, _) in children.items() if pid == group
                      and all(p in children for p, (_, g, _) in table.items() if g == group)}
            for group in groups:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(group, signal.SIGKILL)
            table = _process_table()
            for pid, (group, born) in children.items():
                if group not in groups and table.get(pid, (None, None, None))[2] == born:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream:
                    stream.close()


def _visible_event(event, root):
    kind = event.get("type")
    if kind == "message_end":
        message = event.get("message", {})
        if message.get("role") == "assistant":
            if message.get("stopReason") in ("error", "aborted"):
                return {"error": True}
            return {"text": "\n".join(c.get("text", "") for c in message.get("content", [])
                                      if c.get("type") == "text")}
    if kind == "tool_execution_start":
        tool, args = event.get("toolName"), event.get("args", {})
        record = {"event": "start", "call": event.get("toolCallId"), "tool": tool}
        if tool in ("read", "edit", "write") and isinstance(args.get("path"), str):
            path = (Path(root) / Path(args["path"]).expanduser()).resolve()
            record["path"] = str(path)
            if path.is_relative_to(Path(os.environ.get("PI_E2E_ROOT", root)).resolve()) and path.is_file():
                record["sha256"] = _hash(path)
            for key in ("offset", "limit"):
                if isinstance(args.get(key), int):
                    record[key] = args[key]
        return record
    if kind == "tool_execution_end":
        return {"event": "end", "call": event.get("toolCallId"),
                "ok": not bool(event.get("isError"))}
    return None


_DIAGNOSTIC_PATTERNS = {
    "authentication_unavailable": ("no api key", "no models available", "authentication required", "not authenticated"),
    "extension_load_failed": ("failed to load extension", "cannot find module", "cannot find package"),
    "unsupported_argument": ("unknown option", "unknown argument", "unrecognized option"),
    "network_failure": ("fetch failed", "enotfound", "econnrefused", "etimedout"),
}


def _diagnostic_flags(text):
    return [name for name, needles in _DIAGNOSTIC_PATTERNS.items() if any(s in text.lower() for s in needles)]


def _report_signatures(root):
    signatures = {}
    for path in sorted(Path(root).glob("tiny/target/surefire-reports/TEST-*.xml")):
        assert path.resolve().is_relative_to(Path(root).resolve()), "Surefire report escaped fixture"
        stat = path.stat()
        signatures[str(path)] = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    return signatures


def _suites(root, before=None):
    suites = []
    for filename, signature in _report_signatures(root).items():
        if before is not None and before.get(filename) == signature:
            continue
        path = Path(filename)
        assert path.stat().st_size < 2_000_000, "oversized Surefire report"
        data = path.read_bytes()
        assert b"<!DOCTYPE" not in data.upper() and b"<!ENTITY" not in data.upper(), "DTD in Surefire report"
        tree = ET.fromstring(data)
        suites.append({"path": str(path), **{k: int(tree.get(k, "0"))
                       for k in ("tests", "failures", "errors", "skipped")}})
    return suites


def _has_red_assertion(records):
    return any(s["tests"] > 0 and s["failures"] > 0 and s["errors"] == 0
               for r in records if r.get("action") == "MAKER_STEP1_RED"
               for e in r["events"] if e.get("event") == "end" for s in e.get("suites", []))


def _pi_wrapper(args):
    base = Path(os.environ["PI_E2E_ROOT"])
    cwd = Path.cwd().resolve()
    if not cwd.is_relative_to(base) or "--model" in args:
        return 2
    sid = args[args.index("--session-id") + 1]
    evidence = base / "evidence" / (uuid.uuid4().hex + ".json")
    record = {"sid": sid, "cwd": str(cwd), "events": [], "returncode": None, "action": None,
              "mcp_config": args[args.index("--mcp-config") + 1] if "--mcp-config" in args else None,
              "extensions": [args[i + 1] for i, a in enumerate(args[:-1]) if a == "-e"],
              "original_argv_sha256": hashlib.sha256(json.dumps(args).encode()).hexdigest()}
    # build_cmd appends the original JSON user payload; chat/repair/probe turns have no action.
    # Read only that field, never the system prompt, and leave the original argv untouched.
    if "--append-system-prompt" in args:
        with contextlib.suppress(ValueError, TypeError):
            payload = json.loads(args[-1])
            if isinstance(payload, dict) and isinstance(payload.get("action"), str):
                record["action"] = payload["action"]
    bash_reports = {}
    extras = ["--offline", "--no-extensions", "-e", os.environ["PI_E2E_ADAPTER"],
              "--no-skills", "--no-context-files", "--no-prompt-templates", "--no-themes",
              "--no-approve", "--session-dir", str(base / "pi-sessions"), "--mode", "json"]
    for skill in sorted((Path.home() / ".pi/agent/skills").glob("*/SKILL.md")):
        extras += ["--skill", str(skill)]
    _json(evidence, record)
    proc = subprocess.Popen([os.environ["PI_E2E_NATIVE"], *extras, *args],
                            stdin=sys.stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ, "out")
    selector.register(proc.stderr, selectors.EVENT_READ, "err")
    buffer, texts, diagnostic_tail = b"", [], ""
    deadline = time.monotonic() + 590
    try:
        while selector.get_map():
            if time.monotonic() >= deadline:
                record["timed_out"] = True
                return 124
            for key, _ in selector.select(timeout=0.2):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "err":
                    record["stderr_bytes_discarded"] = record.get("stderr_bytes_discarded", 0) + len(chunk)
                    diagnostic_tail = (diagnostic_tail + chunk.decode("utf-8", "replace"))[-8192:]
                    record["diagnostics"] = sorted(set(record.get("diagnostics", [])) | set(_diagnostic_flags(diagnostic_tail)))
                    continue
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    try:
                        visible = _visible_event(json.loads(line), cwd)
                    except (ValueError, TypeError):
                        continue
                    if visible is None:
                        continue
                    if "text" in visible:
                        texts.append(visible["text"])
                    else:
                        if visible.get("event") == "start" and visible.get("tool") == "bash":
                            visible["reports_before"] = _report_signatures(cwd)
                            bash_reports[visible["call"]] = visible["reports_before"]
                        if visible.get("event") == "end" and visible["call"] in bash_reports:
                            visible["suites"] = _suites(cwd, before=bash_reports.pop(visible["call"]))
                            for index, suite in enumerate(visible["suites"]):
                                target = evidence.parent / evidence.stem / f"{len(record['events'])}-{index}.xml"
                                target.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copyfile(suite["path"], target)
                                suite["snapshot"] = str(target)
                        record["events"].append(visible)
                    _json(evidence, record)
        record["returncode"] = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        record["reply"] = str(_write(evidence.with_suffix(".txt"), "\n".join(texts)))
        print("\n".join(texts))
        return record["returncode"] or int(any(e.get("error") for e in record["events"]))
    finally:
        selector.close()
        _json(evidence, record)
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def _isolated_environment(base):
    old = os.environ.copy()
    # pip --user packages resolve under the real HOME; captured before HOME moves below
    native_user_site = Path(site.getusersitepackages())
    native = old.get("LOOP_ENGINE_PI_E2E_NATIVE_BIN") or old.get("LOOP_ENGINE_PI_BIN") or shutil.which("pi")
    native = native or str(Path.home() / ".nvm/versions/node/v22.22.0/bin/pi")
    candidates = [old.get("LOOP_ENGINE_PI_E2E_ADAPTER", ""),
                  str(Path.home() / ".pi/agent/npm/node_modules/pi-mcp-adapter/index.ts"),
                  str(Path(native).resolve().parent / "node_modules/pi-mcp-adapter/index.ts")]
    adapter = next((str(Path(p).resolve()) for p in candidates if p and Path(p).is_file()), "")
    for key in list(os.environ):
        if key.startswith(("LOOP_ENGINE_", "PI_", "XDG_", "MCP_", "MAVEN_", "GIT_", "NPM_CONFIG_", "npm_config_")) or key in {
            "AUDIT_LOG", "SNAP_DIR", "NODE_OPTIONS", "PYTHONPATH", "PYTHONHOME", "JAVA_TOOL_OPTIONS",
            "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CONFIG",
            "AZURE_CONFIG_DIR", "BASH_ENV", "ENV", "ZDOTDIR", "GOOGLE_CLOUD_KEYFILE_JSON",
            "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "AWS_WEB_IDENTITY_TOKEN_FILE"}:
            os.environ.pop(key)
    home = base / "home"
    dirs = {"HOME": home, "XDG_CONFIG_HOME": home / ".config", "XDG_CACHE_HOME": home / ".cache",
            "XDG_DATA_HOME": home / ".local/share", "XDG_STATE_HOME": home / ".local/state",
            "PI_CODING_AGENT_DIR": home / ".pi/agent", "LOOP_ENGINE_DATA_DIR": base / "data",
            "MAVEN_USER_HOME": home / ".m2", "TMPDIR": base / "tmp"}
    for key, path in dirs.items():
        path.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(path)
    if native_user_site.is_dir():
        os.environ["PYTHONPATH"] = str(native_user_site)
    native_agent = Path(old.get("PI_CODING_AGENT_DIR") or "") if old.get("PI_CODING_AGENT_DIR") \
        else Path(old.get("HOME") or "") / ".pi" / "agent"
    linked = ""
    if (native_agent / "auth.json").is_file():
        os.symlink(str(native_agent / "auth.json"), str(home / ".pi/agent" / "auth.json"))
        linked = "1"
    os.environ["PI_E2E_NATIVE_AUTH"] = linked
    # pi resolves its catalog from the agent dir; without these it fails with "No models available"
    # even with valid credentials. Copied rather than symlinked so pi's cache refreshes stay in the tree.
    carried = ""
    for name in ("models.json", "models-store.json", "settings.json"):
        if (native_agent / name).is_file():
            target = home / ".pi/agent" / name
            shutil.copyfile(native_agent / name, target)
            os.chmod(target, 0o600)
            carried = "1"
    os.environ["PI_E2E_NATIVE_CATALOG"] = carried
    settings = _write(base / "maven-settings.xml", '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"/>\n')
    flags = ["-s", str(settings), "-gs", str(settings), f"-Dmaven.repo.local={base / 'm2-repository'}"]
    os.environ.update({"LOOP_ENGINE_PI_E2E": "1", "LOOP_ENGINE_AGENT_CLI": "pi",
                       "LOOP_ENGINE_MODEL_CONFIG": str(_json(base / "models.json", {})),
                       "PI_E2E_ROOT": str(base), "PI_E2E_NATIVE": str(Path(native).absolute()),
                       "PI_E2E_ADAPTER": adapter, "PYTHONDONTWRITEBYTECODE": "1",
                       "MAVEN_SKIP_RC": "true", "MAVEN_ARGS": " ".join(flags),
                       "MAVEN_OPTS": f"-Duser.home={home} -Dmaven.repo.local={base / 'm2-repository'}",
                       "JAVA_TOOL_OPTIONS": f"-Duser.home={home}", "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_CONFIG_GLOBAL": os.devnull})
    wrapper = _write(base / "bin/pi-e2e", "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " "
                     + shlex.quote(str(Path(__file__).resolve())) + ' --pi-wrapper "$@"\n')
    wrapper.chmod(0o700)
    os.environ["LOOP_ENGINE_PI_BIN"] = str(wrapper)
    for name in ("skills", "agents"):
        shutil.copytree(SOURCE / name, home / ".pi/agent" / name)
    return flags


class _Matrix:
    def __init__(self, base, flags):
        self.base, self.flags = base, flags
        self.data = base / "data"
        self.cwd = base
        self.deadline = time.monotonic() + 3540
        self.results = {name: {"status": "blocked", "reason": "not started"}
                        for name in ("maven-green", "C", "pi-preflight", "A", "B", "D")}
        self.calls = []
        _json(base / "results.json", self.results)
        self.boundary = (
            f"This is an opt-in acceptance fixture, NOT a sandbox. All writes, generated files, "
            f"commands and working directories must stay under {base}. Source assets at {SOURCE} "
            "are read-only. Model access is preconfigured for this session: never open, read, print "
            "or copy auth.json or any credential file, personal configuration, print environment "
            "values, access unrelated projects, change permissions/settings, approve work, or git commit/push. "
            "Do not name models or output reasoning/metadata. "
            "Use the native read tool to re-read current spec/plan, never .loop/result*.md as input. "
            "Use only local fixture code and Maven Central dependencies; no business network services. "
            "Do the exact current phase yourself; no nested agents or installers. Do not change specs, "
            "POMs, AGENTS.md or Maven settings. Edit specs only when this turn explicitly asks to. "
            "Implement the supplied Java stub only in GREEN or a dispatched fix phase. No DAO/DB is needed. "
            "Git has an initial index but deliberately no HEAD/commits: review git diff and new untracked tests. "
            "Declare absolute paths for all changed source and test files."
        )

    def run(self, argv, **kwargs):
        remaining = self.deadline - time.monotonic()
        if remaining <= 2:
            raise Blocked("total matrix time budget exhausted")
        kwargs["timeout"] = min(kwargs.get("timeout", 600), 600, remaining)
        kwargs.setdefault("cwd", str(self.cwd))
        pi = str(argv[0]) == os.environ["LOOP_ENGINE_PI_BIN"]
        kind = "pi" if pi else "engine" if str(SOURCE / "__main__.py") in argv else "local"
        record = {"kind": kind, "cwd": str(kwargs["cwd"])}
        if pi:
            record["sid"] = argv[argv.index("--session-id") + 1]
        else:
            record["argv"] = list(map(str, argv))
        log = self.cwd / "diagnostics" / f"process-{len(self.calls):03d}"
        self.calls.append(record)
        try:
            result = _run_process(argv, **kwargs)
            record["rc"] = result.returncode
            if not pi:
                record["stdout"] = str(_write(log.with_suffix(".out"), result.stdout or ""))
                record["stderr"] = str(_write(log.with_suffix(".err"), result.stderr or ""))
            if kind == "engine" and result.stdout:
                with contextlib.suppress(ValueError, TypeError):
                    parsed = json.loads(result.stdout)
                    if isinstance(parsed, dict):
                        record.update({k: parsed[k] for k in ("action", "next_action", "error") if k in parsed})
            return result
        except subprocess.TimeoutExpired:
            record["timed_out"] = True
            raise
        finally:
            _json(self.base / "processes.json", self.calls)

    def fixture(self, name, spec=STRONG_SPEC, smoke=None):
        import registry
        from state import StateManager
        from spec_utils import compute_spec_hash, compute_spec_norm_hash
        root = self.base / name
        root.mkdir(parents=True, exist_ok=True)
        self.cwd = root
        _write(root / SPEC, spec)
        _write(root / JAVA, STUB)
        parent = '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
        _write(root / "pom.xml", parent + '<groupId>example</groupId><artifactId>fixture</artifactId>'
               '<version>1</version><packaging>pom</packaging><modules><module>tiny</module></modules></project>')
        plugins = "".join('<plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-'
                          + plugin + '-plugin</artifactId><version>' + version + '</version></plugin>'
                          for plugin, version in (("clean", "3.2.0"), ("resources", "3.3.1"),
                                                  ("compiler", "3.11.0"), ("surefire", "3.2.5")))
        _write(root / "tiny/pom.xml", parent + '<parent><groupId>example</groupId>'
               '<artifactId>fixture</artifactId><version>1</version></parent><artifactId>tiny</artifactId>'
               '<properties><maven.compiler.source>1.8</maven.compiler.source>'
               '<maven.compiler.target>1.8</maven.compiler.target>'
               '<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>'
               '<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId>'
               '<version>4.13.2</version><scope>test</scope></dependency></dependencies>'
               '<build><plugins>' + plugins + '</plugins></build></project>')
        command = "mvn clean test -B -ntp " + " ".join(self.flags)
        instructions = self.boundary + "\n\nJava 8, JUnit 4. Run exactly:\n" + command + "\n"
        for path in ("AGENTS.md", ".qoder/AGENTS.md"):
            _write(root / path, instructions)
        _write(root / ".mvn/maven.config", "\n".join(self.flags) + "\n")
        _write(root / ".gitignore", "target/\n.loop/\n.codegraph/\ndiagnostics/\n")
        if smoke is not None:
            _write(root / "tiny/src/test/java/example/GateTest.java",
                   'package example; import org.junit.Test; import static org.junit.Assert.*;\n'
                   'public class GateTest { @Test public void gate() { assertEquals(1, '
                   + ("0" if smoke == "red" else "1") + '); } }\n')
        sm = StateManager(str(root))
        state = sm.init_state()
        sm.add_module(state, KEY, "e2e", "tiny", project_roots=[str(root)],
                      spec_hash=compute_spec_hash(str(root / SPEC)),
                      spec_norm_hash=compute_spec_norm_hash(str(root / SPEC)))
        state["modules"][KEY]["status"] = "PARTIAL"
        sm.save(state)
        registry.add_requirement(name, str(root), projects=[{"name": "fixture", "source": str(root)}])
        if shutil.which("git"):
            self.run(["git", "init", "-q", str(root)], check=True)
            self.run(["git", "-C", str(root), "add", "pom.xml", "tiny", "openspec",
                      "AGENTS.md", ".qoder/AGENTS.md", ".mvn/maven.config", ".gitignore"], check=True)
        return root, sm

    def records(self, root):
        records = []
        for path in sorted((self.base / "evidence").glob("*.json"), key=lambda p: p.stat().st_mtime_ns):
            item = json.loads(path.read_text())
            if item["cwd"] == str(root):
                records.append({**item, "evidence": str(path)})
        return records

    def check_read(self, record, path, digest):
        completed = {e["call"] for e in record["events"] if e.get("event") == "end" and e["ok"]}
        lines = len(Path(path).read_text().splitlines())
        covered = set()
        for e in record["events"]:
            if e.get("tool") == "read" and e.get("path") == str(path) and e.get("sha256") == digest and e["call"] in completed:
                start = e.get("offset", 1)
                covered.update(range(start, min(lines + 1, start + e.get("limit", 2000))))
        assert set(range(1, lines + 1)) <= covered, "no successful native re-read covering the current spec"
        assert not any(e.get("tool") == "read" and Path(e.get("path", "")).name.startswith("result")
                       and "/.loop/" in e.get("path", "") for e in record["events"]), "stale output read as input"

    def case(self, name, function, prerequisite=None):
        self.cwd = self.base / name
        self.cwd.mkdir(exist_ok=True)
        result = {"status": "blocked", "reason": prerequisite or "not completed", "root": str(self.cwd)}
        self.results[name] = result
        _json(self.cwd / "result.json", result)
        _json(self.base / "results.json", self.results)
        try:
            if prerequisite:
                raise Blocked(prerequisite)
            function()
            result.update(status="pass", reason="all case assertions satisfied")
        except (Blocked, subprocess.TimeoutExpired) as exc:
            result["reason"] = str(exc) if isinstance(exc, Blocked) else "child process timeout; see processes.json"
        except Exception as exc:
            result.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
        result["evidence"] = [r["evidence"] for r in self.records(self.cwd)] + [
            str(self.cwd / p) for p in (".loop/state.json", "surefire.json", "sync_test_report.json",
                                        "run.json", "turns.json", "audit-evidence.json", "weak-result.json",
                                        "weak-run.json", "strong-run.json") if (self.cwd / p).is_file()]
        result["processes"] = str(self.base / "processes.json")
        self.results[name] = result
        _json(self.base / name / "result.json", result)
        _json(self.base / "results.json", self.results)
        return None if result["status"] == "pass" else f"{name}: {result['reason']}"

    def gate(self, name, red=False):
        from machine import StateMachine
        if not shutil.which("mvn") or not shutil.which("java"):
            raise Blocked("Maven/java unavailable; no test execution claimed")
        root, sm = self.fixture(name, smoke="red" if red else "green")
        state = sm.load()
        mod = state["modules"][KEY]
        mod["files_created"] = [str(root / "tiny/src/test/java/example/GateTest.java")]
        StateMachine(str(root))._execute_synced(state, KEY, mod)
        sm.save(state)
        suites = _suites(root)
        _json(root / "surefire.json", suites)
        if not suites or not sum(s["tests"] for s in suites):
            raise Blocked("Maven did not execute JUnit; inspect diagnostics and sync_test_report (dependency/toolchain failure)")
        assert not sum(s["errors"] + s["skipped"] for s in suites), "JUnit execution errors/skips"
        if red:
            assert sum(s["failures"] for s in suites) > 0, "red gate did not run a failing assertion"
            assert mod["status"] == "BLOCKED", "red gate incorrectly synced"
            report = mod["sync_test_report"][str(root)]
            assert isinstance(report["rc"], int) and report["rc"] != 0, "no real nonzero Maven rc"
            _json(root / "sync_test_report.json", mod["sync_test_report"])
        else:
            assert not sum(s["failures"] for s in suites), "green control failed"
            assert mod["status"] == "SYNCED" and "sync_test_report" not in mod, "green gate failed"

    def preflight(self):
        import agent_cli
        root = self.cwd
        if not os.access(os.environ["PI_E2E_NATIVE"], os.X_OK):
            raise Blocked("native pi binary unavailable; set LOOP_ENGINE_PI_E2E_NATIVE_BIN")
        if not os.environ["PI_E2E_ADAPTER"] or not shutil.which("codegraph"):
            raise Blocked("MCP adapter/codegraph unavailable; set LOOP_ENGINE_PI_E2E_ADAPTER or PATH")
        probe = _write(root / "PROBE.txt", "ENV_AUTH_READY\n")
        prompt = (f"Read {probe} using the native read tool. Use the mcp tool to discover codegraph "
                  "tools (read-only, no indexing required). Then reply ENV_AUTH_READY. Do not write files.")
        result = self.run(agent_cli.build_cmd(str(root), str(uuid.uuid4()), self.boundary, prompt), timeout=120)
        records = self.records(root)
        flags = sorted({flag for r in records for flag in r.get("diagnostics", [])
                        if isinstance(flag, str) and flag in _DIAGNOSTIC_PATTERNS})
        diagnostics = "; diagnostics=" + ",".join(flags) if flags else ""
        if result.returncode or not records or "ENV_AUTH_READY" not in result.stdout:
            state_note = ("native pi login reused via symlink" if os.environ.get("PI_E2E_NATIVE_AUTH")
                          else "no native pi login found; log in on this machine first")
            if not os.environ.get("PI_E2E_NATIVE_CATALOG"):
                state_note += "; no native model catalog/settings to copy"
            raise Blocked("pi preflight produced no authenticated visible probe; "
                          + state_note + "; CLI/adapter may be unavailable; credential contents never read"
                          + diagnostics)
        try:
            self.check_read(records[-1], probe, _hash(probe))
            events = records[-1]["events"]
            successful = {e["call"] for e in events if e.get("event") == "end" and e["ok"]}
            assert any(e.get("tool") == "mcp" and e["call"] in successful for e in events)
            assert records[-1]["mcp_config"] == agent_cli._MCP_CONFIG
        except AssertionError:
            raise Blocked("pi preflight lacks successful native read/MCP evidence; stop costly agent cases" + diagnostics)

    def score(self):
        import scheduler
        root, sm = self.fixture("A", "# Double controller\nTODO: define doubling API.\n")
        weak_hash = _hash(root / SPEC)
        with patch.object(scheduler, "MAX_TOTAL_STEPS", 1):
            first = scheduler.run_requirement("A", module=KEY)
            weak = sm.load()["modules"][KEY]
            _json(root / "weak-state.json", weak)
            _json(root / "weak-run.json", first)
            assert first.get("steps") == 1 and weak["status"] == "NEEDS_REFINEMENT", "weak spec not genuinely scored"
            first_records = self.records(root)
            assert first_records, "no first SCORE process"
            self.check_read(first_records[0], root / SPEC, weak_hash)
            old_output = (root / ".loop/result-SCORE.md").read_bytes()
            (root / "weak-result.json").write_bytes(old_output)
            _write(root / SPEC, STRONG_SPEC)
            strong_hash = _hash(root / SPEC)
            second = scheduler.run_requirement("A", module=KEY)
        strong = sm.load()["modules"][KEY]
        _json(root / "strong-run.json", second)
        records = self.records(root)
        expected_sid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{root}:{KEY}"))
        assert all(r["sid"] == expected_sid for r in records), "SCORE session was not reused"
        assert len(records) > len(first_records), "no second real SCORE turn"
        self.check_read(records[len(first_records)], root / SPEC, strong_hash)
        assert second.get("steps") == 1 and strong["last_score"] >= 90 > weak["last_score"], "real SCORE did not distinguish specs"
        assert old_output != (root / ".loop/result-SCORE.md").read_bytes(), "SCORE output reused"
        assert _hash(root / SPEC) == strong_hash, "scorer edited strong spec"

    def green(self):
        import scheduler
        root, sm = self.fixture("B")
        if not shutil.which("git"):
            raise Blocked("git unavailable for initial index and real review diff")
        original = _hash(root / JAVA)
        compile_result = self.run(["mvn", "-B", "-ntp", "clean", "compile", *self.flags])
        assert compile_result.returncode == 0, "stub does not compile"
        run = scheduler.run_requirement("B", module=KEY)
        _json(root / "run.json", run)
        mod = sm.load()["modules"][KEY]
        assert mod["status"] == "SYNCED", f"full loop did not sync: {run}, status={mod['status']}"
        assert "sync_test_report" not in mod and mod.get("maker_attempt", 0) > 0, "missing real maker/green gate"
        assert _hash(root / JAVA) != original and _hash(root / SPEC) == hashlib.sha256(STRONG_SPEC.encode()).hexdigest(), "implementation missing or spec changed"
        calls = [c for c in self.calls if c["cwd"] == str(root) and c["kind"] == "engine"]
        phases = [c.get("action") for c in calls if "next" in c["argv"]]
        needed = ["SCORE", "MAKER_STEP0", "MAKER_STEP1_RED", "MAKER_STEP2_GREEN", "CHECKER", "CODE_REVIEW"]
        cursor = 0
        for phase in phases:
            if cursor < len(needed) and phase == needed[cursor]:
                cursor += 1
        assert cursor == len(needed), f"missing real phase(s): {phases}"
        final = [c for c in calls if c.get("next_action") == "_SYNCED_"]
        assert final and "[OK] Final test run passed" in Path(final[-1]["stderr"]).read_text(), "no actual final gate evidence"
        suites = _suites(root)
        _json(root / "surefire.json", suites)
        assert sum(s["tests"] for s in suites) > 0 and not sum(s["failures"] + s["errors"] + s["skipped"] for s in suites), "no all-green executed JUnit suite"
        assert _has_red_assertion(self.records(root)), "no fresh MAKER_STEP1_RED assertion evidence captured"
        assert list(root.glob("tiny/src/test/java/**/*.java")), "pi did not write tests"

    def audit(self):
        import registry
        from wecom_server import router
        from spec_utils import compute_spec_hash
        root, sm = self.fixture("D")
        before = (root / SPEC).read_bytes()
        state = sm.load()
        old_hash = state["modules"][KEY]["spec_hash"]
        state["modules"][KEY]["status"] = "SYNCED"
        sm.save(state)
        original = router._run_llm_turn
        turns = []

        def recorded_turn(sid, is_new, prompt, settings=None, **kwargs):
            reply = original(sid, is_new, prompt, settings, **kwargs)
            payloads = router._json_action_payloads(reply)
            if any(p.get("action") != "spec_result" or p.get("requirement") != "D"
                   or p.get("module") != KEY for p in payloads):
                raise AssertionError("unsafe/unrelated reply action refused before dispatch")
            turn = {"sid": sid, "is_new": is_new, "correction": prompt.startswith("[系统纠正]"),
                    "reply": str(_write(root / f"turn-{len(turns) + 1}.txt", reply)), "injected_missing_registration": False}
            if not turns and payloads:
                reply = router._JSON_ACTION_RE.sub("", reply).strip()
                turn["injected_missing_registration"] = True
                # A whitespace reply stays truthy if the genuine first answer was action-only.
                reply = reply or "\n"
            turns.append(turn)
            _json(root / "turns.json", turns)
            return reply

        message = (f"D: Edit the existing spec at {root / SPEC} with the native edit tool, once only: "
                   "change the valid positive example from body 7 / response 14 to body 8 / response 16. "
                   f"Register e2e/tiny via spec_result for requirement D; do NOT approve or implement anything. {self.boundary}")
        with patch.object(router, "_run_llm_turn", recorded_turn):
            reply = router._llm_dispatch(message, registry.list_requirements(), str(self.data), "pi-e2e")
        _write(root / "reply.txt", reply)
        assert len(turns) >= 2 and turns[1]["correction"], "real router correction/second turn did not occur"
        sid = turns[0]["sid"]
        assert all(t["sid"] == sid for t in turns), "correction changed session"
        records = self.records(root)
        bridge = str(SOURCE / "wecom_server/hooks/pi_audit_bridge.ts")
        assert len(records) >= 2 and all(bridge in r["extensions"] and r["sid"] == sid for r in records), "builder did not load source audit bridge/session"
        events = [e for r in records for e in r["events"]]
        completed = {e["call"] for e in events if e.get("event") == "end" and e["ok"]}
        assert any(e.get("tool") == "edit" and e.get("path") == str(root / SPEC)
                   and e["call"] in completed for e in events), "no successful native spec edit"
        snaps = list((self.data / "spec-snapshots").glob(f"*-{sid}-tiny.md"))
        assert any(p.read_bytes() == before for p in snaps), "no matching-session pre-edit snapshot"
        mod = sm.load()["modules"][KEY]
        assert (root / SPEC).read_bytes() != before, "spec bytes unchanged"
        assert mod["status"] == "PARTIAL" and old_hash != mod["spec_hash"] == compute_spec_hash(str(root / SPEC)), "real spec_result did not register new hash"
        assert not any(e.get("approved") for e in __import__("scheduler").load_pending().get("pending", [])), "unexpected approval"
        assert Path(registry.REGISTRY_PATH).is_relative_to(self.base), "registry escaped temporary root"
        _json(root / "audit-evidence.json", {"sid": sid, "snapshots": list(map(str, snaps)),
                                            "audit_log": str(self.data / "audit.log"), "registry": registry.REGISTRY_PATH})


def _main(root):
    base = Path(root).resolve()
    if not base.is_relative_to(Path(tempfile.gettempdir()).resolve()) or base.exists() or any(c.isspace() for c in str(base)):
        raise SystemExit("--run requires a fresh, whitespace-free root under the system temporary directory")
    base.mkdir(parents=True)
    os.chmod(base, 0o700)
    matrix = _Matrix(base, [])
    try:
        matrix.flags = _isolated_environment(base)
    except Exception as exc:
        for name in matrix.results:
            matrix.case(name, lambda: None, f"isolation setup failed: {type(exc).__name__}")
        return 2
    sys.path.insert(0, str(SOURCE))
    notifications = []

    def notify(*args, **kwargs):
        notifications.append({"args": args, "kwargs": kwargs})
        _json(base / "notifications.json", notifications)

    with patch.object(subprocess, "run", matrix.run):
        maven_block = matrix.case("maven-green", lambda: matrix.gate("maven-green"))
        matrix.case("C", lambda: matrix.gate("C", red=True))
        try:
            import agent_cli
            import scheduler
        except ImportError as exc:
            for name in ("pi-preflight", "A", "B", "D"):
                matrix.case(name, lambda: None, f"engine dependency unavailable: {exc.name}")
        else:
            with patch.object(agent_cli, "_pi_model", lambda step=None: ""), \
                    patch.object(scheduler, "MAX_TOTAL_STEPS", 8), \
                    patch.object(scheduler, "STEP_TIMEOUT_SECONDS", 600), \
                    patch.object(scheduler, "LOOP_AGENT_PROMPT", scheduler.LOOP_AGENT_PROMPT + "\n" + matrix.boundary), \
                    patch.object(scheduler, "notify_text", notify), \
                    patch.object(scheduler, "notify_pending", notify):
                pi_block = matrix.case("pi-preflight", matrix.preflight, maven_block)
                matrix.case("A", matrix.score, pi_block)
                matrix.case("B", matrix.green, pi_block)
                matrix.case("D", matrix.audit, pi_block)
    print(f"Evidence retained: {base / 'results.json'}")
    return 1 if any(r["status"] == "failed" for r in matrix.results.values()) else 2 if any(
        r["status"] == "blocked" for r in matrix.results.values()) else 0


def test_visible_events_exclude_reasoning_and_private_metadata(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text("strong spec", encoding="utf-8")
    event = {"type": "message_end", "message": {
        "role": "assistant", "model": "PRIVATE_MODEL", "usage": {"cost": 123},
        "content": [{"type": "thinking", "thinking": "PRIVATE_REASONING"},
                    {"type": "text", "text": "visible answer"}]}}
    assert _visible_event(event, tmp_path) == {"text": "visible answer"}
    assert _visible_event({"type": "message_update", "delta": "PRIVATE"}, tmp_path) is None
    read = _visible_event({"type": "tool_execution_start", "toolCallId": "r1",
                           "toolName": "read", "args": {"path": str(spec),
                           "secret": "PRIVATE"}}, tmp_path)
    assert read["path"] == str(spec)
    assert read["sha256"] == "4b74335046b66a1a77e2fdfd2cef3de8720e5e89c549b5b9047496fe3c7ef592"
    assert "PRIVATE" not in json.dumps(read)
    import pytest
    end = {"event": "end", "call": "r1", "ok": True}
    _Matrix.check_read(None, {"events": [read, end]}, spec, read["sha256"])
    for events in ([read, dict(end, ok=False)], [dict(read, limit=0), end]):
        with pytest.raises(AssertionError):
            _Matrix.check_read(None, {"events": events}, spec, read["sha256"])


def test_startup_diagnostics_are_category_only():
    assert _diagnostic_flags("No API key for PRIVATE; Failed to load extension SECRET") == [
        "authentication_unavailable", "extension_load_failed"]
    assert _diagnostic_flags("unrelated secret PRIVATE") == []


def test_wrapper_reports_are_call_fresh_and_phase_specific(tmp_path, monkeypatch):
    import agent_cli
    monkeypatch.chdir(tmp_path)
    for key, value in {"HOME": str(tmp_path), "PI_E2E_ROOT": str(tmp_path),
                       "PI_E2E_NATIVE": sys.executable, "PI_E2E_ADAPTER": "offline-adapter",
                       "LOOP_ENGINE_AGENT_CLI": "pi", "LOOP_ENGINE_PI_BIN": "offline-pi"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(agent_cli, "_pi_model", lambda action=None: "")
    original_popen, original_visible = subprocess.Popen, _visible_event
    report = tmp_path / "tiny/target/surefire-reports/TEST-Gate.xml"
    xml = '<testsuite tests="1" failures="1" errors="0"><testcase><failure type="java.lang.AssertionError"/></testcase></testsuite>'
    events = [{"type": f"tool_execution_{kind}", "toolName": "bash", "toolCallId": call}
              for call in ("stale", "fresh") for kind in ("start", "end")]
    records = []
    for action in ("MAKER_STEP2_GREEN", "MAKER_STEP1_RED"):
        _write(report, xml)
        args = agent_cli.build_cmd(str(tmp_path), str(uuid.uuid4()),
                                  'PRIVATE_PROMPT {"action":"SCORE"}',
                                  json.dumps({"action": action, "directives": {"private": "PRIVATE_PAYLOAD"}}))[1:]
        unchanged = list(args)

        def fake_native(argv, **kwargs):
            assert argv[-len(args):] == unchanged, "wrapper altered original builder argv"
            return original_popen([sys.executable, "-c", "print(" + repr(
                "\n".join(json.dumps(e) for e in events)) + ")"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def observe(event, root):
            if event["type"] == "tool_execution_end" and event["toolCallId"] == "fresh":
                _write(report, xml)  # Same report/content, rewritten during this bash call.
            return original_visible(event, root)

        with patch.object(subprocess, "Popen", fake_native), \
                patch.dict(_pi_wrapper.__globals__, _visible_event=observe):
            assert _pi_wrapper(args) == 0
        record = next(json.loads(p.read_text()) for p in (tmp_path / "evidence").glob("*.json")
                      if json.loads(p.read_text())["sid"] == args[args.index("--session-id") + 1])
        ends = [e for e in record["events"] if e.get("event") == "end"]
        assert not ends[0].get("suites"), "unchanged preexisting RED report was attributed to bash"
        assert len(ends[1]["suites"]) == 1 and Path(ends[1]["suites"][0]["snapshot"]).read_text() == xml
        assert record["action"] == action and args == unchanged
        assert "PRIVATE" not in json.dumps(record)
        records.append(record)
    assert not _has_red_assertion(records[:1]), "GREEN failure counted as RED-phase evidence"
    assert _has_red_assertion(records[1:]), "fresh RED-phase assertion was not accepted"


def test_preflight_blocked_reason_only_includes_allowlisted_diagnostics(tmp_path, monkeypatch):
    import agent_cli
    import pytest
    matrix = _Matrix(tmp_path, [])
    monkeypatch.setenv("PI_E2E_NATIVE", "offline-pi")
    monkeypatch.setenv("PI_E2E_ADAPTER", "offline-adapter")
    monkeypatch.setattr(os, "access", lambda *args: True)
    monkeypatch.setattr(shutil, "which", lambda *args: "offline-codegraph")
    monkeypatch.setattr(agent_cli, "build_cmd", lambda *args: ["offline-pi"])
    monkeypatch.setattr(matrix, "records", lambda root: [{
        "events": [], "diagnostics": ["authentication_unavailable", "extension_load_failed", "PRIVATE"]}])
    for rc, stdout in ((1, ""), (0, "ENV_AUTH_READY")):
        monkeypatch.setattr(matrix, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], rc, stdout, ""))
        with pytest.raises(Blocked) as exc:
            matrix.preflight()
        reason = str(exc.value)
        assert "authentication_unavailable" in reason and "extension_load_failed" in reason
        assert "PRIVATE" not in reason


def test_process_owner_matches_exact_tokens_and_birth_times():
    owner = uuid.uuid4().hex
    children = []
    try:
        for marker in ({_OWNER_ENV: owner}, {_OWNER_ENV: owner + "suffix"}, {"OTHER" + _OWNER_ENV: owner}):
            env = dict(os.environ)
            env.pop(_OWNER_ENV, None)
            children.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                             env={**env, **marker}, stdin=subprocess.DEVNULL,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        owned = _owned_processes(owner)
        assert owned == {children[0].pid: _process_table()[children[0].pid][2]}
        _run_process([sys.executable, "-c", "pass"], timeout=5, check=True)
        assert all(child.poll() is None for child in children), "unrelated invocation killed a sibling"
    finally:
        for child in children:
            child.kill()
            child.wait()
    seen = {100: (100, "original-birth")}
    _remember_children(100, seen, {100: (1, 100, "reused-birth"), 101: (100, 100, "child-birth")})
    assert 101 not in seen, "reused parent PID claimed an unrelated child"


def test_process_owner_replaced_without_mutating_supplied_environment(monkeypatch):
    inherited = uuid.uuid4().hex
    monkeypatch.setenv(_OWNER_ENV, inherited)
    monkeypatch.setenv("E2E_AUTH_SENTINEL", "must-not-leak-into-empty-env")
    code = ("import json,os; "
            "print(json.dumps({'marked':bool(os.environ.get('_PI_E2E_PROCESS_OWNER')),"
            "'replaced':os.environ.get('_PI_E2E_PROCESS_OWNER')!=os.environ.get('E2E_OLD_OWNER'),"
            "'auth':'E2E_AUTH_SENTINEL' in os.environ}))")
    for env in ({}, {_OWNER_ENV: inherited, "E2E_OLD_OWNER": inherited}):
        before = dict(env)
        result = _run_process([sys.executable, "-c", code], env=env, timeout=5, check=True)
        assert json.loads(result.stdout) == {"marked": True, "replaced": True, "auth": False}
        assert env == before and os.environ[_OWNER_ENV] == inherited


def test_process_timeout_cleans_spawned_tree(tmp_path):
    pid_file = tmp_path / "pid"
    code = ("import subprocess,sys,time; from pathlib import Path; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
            "start_new_session=True); Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)")
    import pytest
    with pytest.raises(subprocess.TimeoutExpired):
        _run_process([sys.executable, "-c", code, str(pid_file)], timeout=1)
    pid = int(pid_file.read_text())
    status = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                            capture_output=True, text=True).stdout.strip()
    assert not status or status.startswith("Z"), status


def test_process_completion_cleans_immediately_reparented_child(tmp_path, monkeypatch):
    pid_file = tmp_path / "orphan-pid"
    owner = uuid.uuid4()
    monkeypatch.setattr(uuid, "uuid4", lambda: owner)
    code = ("import subprocess,sys; from pathlib import Path; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
            "start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "Path(sys.argv[1]).write_text(str(p.pid))")
    try:
        _run_process([sys.executable, "-c", code, str(pid_file)], timeout=5, check=True,
                     env={**os.environ, _OWNER_ENV: owner.hex})
        pid = int(pid_file.read_text())
        status = _ORIGINAL_RUN(["ps", "-p", str(pid), "-o", "stat="],
                               capture_output=True, text=True).stdout.strip()
        assert not status or status.startswith("Z"), "detached orphan survived completion"
    finally:
        # Clean up a failed reproducer too, without trusting a possibly reused PID file.
        owned = _owned_processes(owner.hex)
        table = _process_table()
        for pid, born in owned.items():
            if table.get(pid, (None, None, None))[2] == born:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)


def test_child_isolation_and_real_fixture_scoping(tmp_path):
    base = tmp_path / "isolated"
    env = dict(os.environ, E2E_AUTH_SENTINEL="env-only", PI_MCP_CONFIG_MODE="exclusive",
               GOOGLE_APPLICATION_CREDENTIALS=str(tmp_path / "must-not-read.json"),
               GIT_INDEX_FILE=str(tmp_path / "must-not-write"), BASH_ENV=str(tmp_path / "must-not-source"))
    code = (
        "import json,os,runpy,sys; from pathlib import Path; "
        "n=runpy.run_path(sys.argv[1]); base=Path(sys.argv[2]); base.mkdir(); "
        "flags=n['_isolated_environment'](base); sys.path.insert(0,str(n['SOURCE'])); "
        "import constants,agent_cli,spec_utils; m=n['_Matrix'](base,flags); "
        "root,sm=m.fixture('offline'); cmd=spec_utils.read_test_commands([str(root)]); "
        "scoped=spec_utils.read_synced_test_commands(cmd,[str(root)],[str(root/n['JAVA'])]); "
        "print(json.dumps({'data':constants.DATA_DIR,'home':os.environ['HOME'],"
        "'model_config':json.loads(Path(agent_cli._model_config_path()).read_text()),"
        "'env_auth':os.environ.get('E2E_AUTH_SENTINEL')=='env-only',"
        "'credential_file':'GOOGLE_APPLICATION_CREDENTIALS' in os.environ,"
        "'exclusive':'PI_MCP_CONFIG_MODE' in os.environ,'command':scoped[str(root)]}))"
    )
    result = _run_process([sys.executable, "-c", code, str(Path(__file__).resolve()), str(base)],
                          env=env, timeout=20, check=True)
    info = json.loads(result.stdout)
    assert info["data"] == str(base / "data")
    assert info["home"] == str(base / "home")
    assert info["model_config"] == {} and info["env_auth"]
    assert not info["credential_file"] and not info["exclusive"]
    assert " -pl tiny -am" in info["command"]
    assert f"-s {base}/maven-settings.xml -gs {base}/maven-settings.xml" in info["command"]
    assert f"-Dmaven.repo.local={base}/m2-repository" in info["command"]
    assert not (tmp_path / "must-not-write").exists()


def test_script_refuses_execution_without_opt_in(tmp_path):
    root = tmp_path / "unused"
    result = _run_process([sys.executable, str(Path(__file__).resolve()), "--run", str(root)],
                          env=dict(os.environ, LOOP_ENGINE_PI_E2E="0"), timeout=10)
    assert result.returncode != 0 and "BLOCKED" in result.stderr
    assert not root.exists()


def test_real_pi_matrix(tmp_path):
    import pytest
    if os.environ.get("LOOP_ENGINE_PI_E2E") != "1":
        pytest.skip("opt-in: LOOP_ENGINE_PI_E2E=1; real pi/Maven, not a sandbox")
    root = tmp_path / "pi-e2e"
    result = _run_process([sys.executable, str(Path(__file__).resolve()),
                           "--run", str(root)], timeout=3600, ceiling=3600)
    report = root / "results.json"
    assert report.is_file(), f"runner exited {result.returncode}; evidence: {root}"
    results = json.loads(report.read_text())
    failures = [k for k, v in results.items() if v["status"] == "failed"]
    assert not failures, f"failed: {failures}; evidence: {report}"
    blocked = [k for k, v in results.items() if v["status"] == "blocked"]
    if blocked:
        pytest.skip(f"BLOCKED: {blocked}; evidence retained: {report}")
    assert result.returncode == 0, f"runner exit {result.returncode}; evidence: {report}"


if __name__ == "__main__":
    if os.environ.get("LOOP_ENGINE_PI_E2E") != "1":
        raise SystemExit("BLOCKED: set LOOP_ENGINE_PI_E2E=1 to opt in to real pi/Maven")
    if len(sys.argv) == 3 and sys.argv[1] == "--run":
        raise SystemExit(_main(sys.argv[2]))
    if len(sys.argv) > 2 and sys.argv[1] == "--pi-wrapper" and os.environ.get("PI_E2E_ROOT"):
        raise SystemExit(_pi_wrapper(sys.argv[2:]))
    raise SystemExit("Usage: python tests/test_pi_e2e.py --run <fresh-system-temp-root>")
