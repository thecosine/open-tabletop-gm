"""Focused Party Input composer and multiline transport tests."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parent.parent
DISPLAY = REPO / "display"
TEMPLATE = DISPLAY / "templates" / "index.html"
sys.path.insert(0, str(DISPLAY))


class _CombatReconnectModel:
    """Executable model of the browser's generation-bound reconnect policy."""

    def __init__(self, *, local_full: bool):
        self.local_full = local_full
        self.campaign = ""
        self.generation = 0
        self.token_generation = -1
        self.claim_generation = -1

    def begin(self, campaign: str):
        if campaign == self.campaign:
            return
        self.campaign = campaign
        self.generation += 1
        self.token_generation = -1
        self.claim_generation = -1

    def claim(self):
        self.token_generation = self.generation
        self.claim_generation = self.generation

    def reconnect(self, projection_status=200):
        actions = []
        if self.token_generation == self.generation:
            actions.append("projection")
            if projection_status not in (401, 403):
                return actions
            self.token_generation = -1
        if self.local_full and self.claim_generation != self.generation:
            actions.extend(("bootstrap", "projection-retry"))
        else:
            actions.append("claim-required")
        return actions


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ComposerMarkupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = TEMPLATE.read_text(encoding="utf-8")
        match = re.search(r'<textarea id="player-input-text".*?</textarea>', cls.source, re.DOTALL)
        assert match is not None
        cls.textarea = match.group(0)

    def test_textarea_has_no_restrictive_maxlength(self):
        self.assertNotIn("maxlength=", self.textarea)
        self.assertIn('rows="4"', self.textarea)
        self.assertIn('for="player-input-text"', self.source)

    def test_textarea_has_minimum_and_maximum_sizing(self):
        rule = re.search(r"#player-input-text\s*\{(.*?)\}", self.source, re.DOTALL).group(1)
        self.assertRegex(rule, r"min-height:\s*92px")
        self.assertRegex(rule, r"max-height:\s*min\(38vh, 280px\)")
        self.assertIn("overflow-y: hidden", rule)

    def test_auto_grow_and_reset_hooks_are_present(self):
        self.assertIn("function _resizeInputComposer()", self.source)
        self.assertIn("_inputText.style.height = 'auto'", self.source)
        self.assertIn("_inputText.scrollHeight", self.source)
        self.assertIn("_inputText.style.overflowY", self.source)
        self.assertIn("_inputText.addEventListener('input', _resizeInputComposer)", self.source)
        cleared = self.source.index("_inputText.value = ''")
        self.assertLess(cleared, self.source.index("_resizeInputComposer();", cleared))

    def test_shift_enter_is_newline_and_normal_shortcut_stages(self):
        handler = re.search(
            r"_inputText\.addEventListener\('keydown'.*?\n\}\);", self.source, re.DOTALL
        ).group(0)
        self.assertIn("e.key === 'Enter' && e.shiftKey", handler)
        self.assertIn("e.ctrlKey || e.metaKey", handler)
        self.assertIn("e.preventDefault()", handler)
        self.assertIn("_stageAction()", handler)
        self.assertIn("_stageBtn.addEventListener('click', _stageAction)", self.source)

    def test_send_button_appears_immediately_before_stage_and_invokes_direct_send(self):
        footer = re.search(r'<div id="input-footer".*?</div>', self.source, re.DOTALL).group(0)
        self.assertRegex(footer, r'<button id="send-btn">Send</button>\s*<button id="stage-btn">Stage</button>')
        self.assertIn("_sendBtn.addEventListener('click', _sendAction)", self.source)
        sender = self.source.split("async function _sendAction()", 1)[1].split(
            "// ── Toggle ready", 1
        )[0]
        self.assertIn("fetch('/player-input/send'", sender)

    def test_direct_send_clears_only_after_success_and_preserves_failure_text(self):
        sender = self.source.split("async function _sendAction()", 1)[1].split(
            "// ── Toggle ready", 1
        )[0]
        success = sender.split("if (res.status === 204)", 1)[1].split("return;", 1)[0]
        failure = sender.split("if (res.status === 204)", 1)[1].split("return;", 1)[1]
        self.assertIn("_inputText.value = ''", success)
        self.assertIn("_resizeInputComposer()", success)
        self.assertIn("_inputText.focus({ preventScroll: true })", success)
        self.assertNotIn("_inputText.value = ''", failure)
        self.assertIn("Retry Send", failure)

    def test_ctrl_or_cmd_enter_still_stages_and_shift_enter_still_adds_newline(self):
        handler = re.search(
            r"_inputText\.addEventListener\('keydown'.*?\n\}\);", self.source, re.DOTALL
        ).group(0)
        self.assertIn("if (e.key === 'Enter' && e.shiftKey) return", handler)
        self.assertIn("e.ctrlKey || e.metaKey", handler)
        self.assertIn("_stageAction()", handler)
        self.assertNotIn("_sendAction()", handler)

    def test_empty_input_and_over_limit_input_are_not_submitted(self):
        self.assertIn("if (!text.trim()) {", self.source)
        self.assertIn("if (text.length > _MAX_INPUT_CHARS)", self.source)
        self.assertIn('id="input-character-count"', self.source)
        self.assertIn('aria-describedby="input-shortcut input-character-count input-error"', self.textarea)

    def test_action_feed_preserves_whitespace_and_wraps_without_clipping(self):
        rule = re.search(r"\.action-block p\s*\{(.*?)\}", self.source, re.DOTALL).group(1)
        self.assertIn("min-width: 0", rule)
        self.assertIn("white-space: pre-wrap", rule)
        self.assertIn("overflow-wrap: anywhere", rule)
        for forbidden in ("line-clamp", "max-height", "overflow: hidden", "text-overflow"):
            self.assertNotIn(forbidden, rule)

    def test_action_renderer_uses_the_complete_action_field(self):
        renderer = re.search(
            r"function renderActionBlock\(.*?\n\}", self.source, re.DOTALL
        ).group(0)
        self.assertIn("p.textContent = cleaned", renderer)
        self.assertNotRegex(renderer, r"\.(?:slice|substring|substr)\s*\(")
        self.assertIn("renderActionBlock(payload.action, payload.text, false, payload.campaign_timestamp)", self.source)
        self.assertIn("renderActionBlock(item.action, item.text, true, item.campaign_timestamp)", self.source)

    def test_timestamp_renderer_is_optional_for_legacy_entries(self):
        helper = re.search(
            r"function _appendCampaignTimestamp\(.*?\n\}", self.source, re.DOTALL
        ).group(0)
        self.assertIn("if (!timestamp) return", helper)
        self.assertIn("className = 'campaign-timestamp'", helper)
        self.assertIn("item.campaign_timestamp", self.source)

    def test_standard_narration_dispatch_remains_intact(self):
        self.assertIn("handleIncomingText(payload.text, payload.campaign_timestamp)", self.source)

    def test_typed_combat_browser_api_is_separate_from_narrative_and_dice(self):
        self.assertIn("window.openTabletopCombat", self.source)
        self.assertIn("_combatFetch('/combat/attack'", self.source)
        self.assertIn("_combatFetch('/combat/lifecycle'", self.source)
        self.assertIn("_combatFetch('/combat/projection'", self.source)
        self.assertNotIn('meta name="combat-token"', self.source)
        self.assertIn("function _combatHeaders()", self.source)
        self.assertIn("open-tabletop-combat-projection", self.source)
        self.assertIn("function _renderCombatProjection", self.source)
        self.assertLess(self.source.index("window.openTabletopCombat ="), self.source.index("function _initDicePad()"))
        dice_init = self.source[self.source.index("function _initDicePad()") :]
        self.assertNotIn("window.openTabletopCombat =", dice_init)

    def test_combat_client_reconnect_and_generation_guards_are_mode_independent(self):
        self.assertIn("evtSource.onopen", self.source)
        self.assertIn("let _combatGeneration = 0", self.source)
        self.assertIn("generation !== _combatGeneration", self.source)
        self.assertIn("client_generation", self.source)
        self.assertIn("updateStats({turn_order: null, encounter_actors: []})", self.source)
        retry = re.search(r"async function _combatFetch\(.*?\n\}", self.source, re.DOTALL).group(0)
        self.assertIn("response.status === 401 || response.status === 403", retry)
        self.assertIn("if (!_canBootstrapCombatDevice()) return response", retry)
        self.assertIn("_combatFetch(path, options, false)", retry)
        self.assertIn("generation === _combatGeneration", retry)
        self.assertNotIn("_combatFetch(path, options, true)", retry)

    def test_combat_bootstrap_waits_for_one_coherent_sse_campaign_generation(self):
        onopen = self.source.split("evtSource.onopen = () => {", 1)[1].split("};", 1)[0]
        self.assertNotIn("_bootstrapCombatDevice", onopen)
        self.assertNotIn("openTabletopCombat.projection", onopen)
        accept = re.search(
            r"function _acceptCombatCampaignPayload\(payload\) \{.*?\n\}", self.source, re.DOTALL
        ).group(0)
        for required in (
            "generation !== _displayGeneration", "combatCampaign !== campaign",
            "_combatSseGeneration === generation && _combatSseCampaign === campaign",
            "_combatSseGeneration = generation", "_combatSseCampaign = campaign",
            "_beginCombatCampaign(campaign)", "_hasCurrentCombatCapability()",
            "window.openTabletopCombat.projection()", "_canBootstrapCombatDevice()",
            "_bootstrapCombatDevice()",
        ):
            self.assertIn(required, accept)
        handler = self.source.split("evtSource.onmessage = (e) => {", 1)[1].split(
            "evtSource.onerror", 1
        )[0]
        self.assertIn("_acceptCombatCampaignPayload(payload)", handler)
        self.assertLess(handler.index("incomingGeneration < _displayGeneration"), handler.index("_acceptCombatCampaignPayload(payload)"))

    def test_claimed_reconnect_projects_first_and_never_falls_into_loopback_bootstrap(self):
        remote = _CombatReconnectModel(local_full=False)
        remote.begin("alpha")
        remote.claim()
        self.assertEqual(remote.reconnect(), ["projection"])
        self.assertEqual(remote.reconnect(403), ["projection", "claim-required"])
        self.assertEqual(remote.reconnect(), ["claim-required"])

        accept = re.search(
            r"function _acceptCombatCampaignPayload\(payload\) \{.*?\n\}", self.source, re.DOTALL
        ).group(0)
        self.assertLess(accept.index("_hasCurrentCombatCapability()"), accept.index("_canBootstrapCombatDevice()"))
        retry = re.search(r"async function _combatFetch\(.*?\n\}", self.source, re.DOTALL).group(0)
        self.assertLess(retry.index("_combatTokenGeneration = -1"), retry.index("_canBootstrapCombatDevice()"))
        bootstrap_guard = re.search(
            r"function _canBootstrapCombatDevice\(\) \{.*?\n\}", self.source, re.DOTALL
        ).group(0)
        self.assertIn("loopback", bootstrap_guard)
        self.assertIn("_combatClaimGeneration !== _combatGeneration", bootstrap_guard)

    def test_local_bootstrap_and_campaign_switch_reset_are_preserved(self):
        local = _CombatReconnectModel(local_full=True)
        local.begin("alpha")
        self.assertEqual(local.reconnect(), ["bootstrap", "projection-retry"])
        local.claim()
        local.begin("beta")
        self.assertEqual(local.reconnect(), ["bootstrap", "projection-retry"])

        begin = re.search(r"function _beginCombatCampaign\(.*?\n\}", self.source, re.DOTALL).group(0)
        for reset in (
            "_combatToken = null", "_combatTokenGeneration = -1",
            "_combatClaimGeneration = -1", "_resetCombatProjection()",
        ):
            self.assertIn(reset, begin)

    def test_combat_projection_matches_encounter_schema_without_inventing_initiative(self):
        projection = self.source[self.source.index("function _renderCombatProjection(receipt)") :]
        projection = projection[:projection.index("window.addEventListener('open-tabletop-combat-projection'")]
        for required in (
            "name: actor.display_name", "disposition: actor.kind === 'enemy' ? 'hostile' : 'neutral'",
            "hp_known: Boolean(actor.hp)", "hp: actor.hp ? {current: actor.hp.current, max: actor.hp.maximum}",
            "ac_known: actor.ac != null", "conditions:",
        ):
            self.assertIn(required, projection)
        self.assertIn("actor.kind === 'enemy' || actor.kind === 'npc'", projection)
        self.assertNotIn("disposition: actor.kind === 'enemy' ? 'hostile' : 'friendly'", projection)
        self.assertNotIn("const order = Object.entries(combatants)", projection)
        self.assertNotIn("turn_order: {order", projection)
        self.assertIn("turnUpdate.current = active ? active.display_name : null", projection)

    def test_remote_claim_is_explicit_secure_and_bootstrap_failures_are_visible(self):
        self.assertIn("claimCapability: async claim =>", self.source)
        self.assertIn("if (!window.isSecureContext)", self.source)
        self.assertIn("open-tabletop-combat-bootstrap-failed", self.source)
        self.assertIn("catch(_reportCombatBootstrapFailure)", self.source)
        self.assertNotIn("catch(() => null);\n    }\n    if (payload.combat_campaign)", self.source)

    def test_idle_combat_projection_resets_without_reporting_bootstrap_failure(self):
        projection = re.search(
            r"projection: async \(\) => \{.*?\n  \},", self.source, re.DOTALL
        ).group(0)
        self.assertIn("if (body.status === 'idle')", projection)
        self.assertIn("_resetCombatProjection()", projection)
        self.assertIn("return null", projection)
        self.assertLess(projection.index("body.status === 'idle'"), projection.index("open-tabletop-combat-projection"))
        self.assertNotIn("_reportCombatBootstrapFailure", projection)

    def test_campaign_switch_rebootstraps_combat_and_clears_encounter_projection(self):
        self.assertIn("_acceptCombatCampaignPayload(payload)", self.source)
        self.assertIn("_bootstrapCombatDevice()", self.source)
        app_source = (DISPLAY / "gm-display-app.py").read_text(encoding="utf-8")
        self.assertIn('"turn_order": None', app_source)
        self.assertIn('"encounter_actors": []', app_source)


class CertificateServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server_module = _load_module(DISPLAY / "cert_server.py", "display_certificate_server_test")

    def test_helper_serves_only_public_certificate(self):
        handler = self.server_module.CertificateHandler
        handler.certificate = b"PUBLIC CERTIFICATE"
        server = self.server_module.http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(f"{base}/cert.pem") as response:
                self.assertEqual(response.read(), b"PUBLIC CERTIFICATE")
                self.assertEqual(response.headers["Cache-Control"], "no-store")
            for forbidden in ("/key.pem", "/.token", "/", "/stats.json"):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(base + forbidden)
                self.assertEqual(raised.exception.code, 404)
                raised.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_pem_reader_rejects_symlinks_and_oversized_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "cert.pem"
            target.write_text("certificate", encoding="ascii")
            link = root / "linked.pem"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                self.server_module._read_pem(link)
            target.write_bytes(b"x" * (self.server_module.MAX_PEM_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "size"):
                self.server_module._read_pem(target)

    def test_private_key_permissions_and_envelope_are_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "key.pem"
            key.write_text("not a key", encoding="ascii")
            key.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "permissions"):
                self.server_module.validate_private_key(key)
            key.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "envelope"):
                self.server_module.validate_private_key(key)

    def test_valid_certificate_and_private_key_pass_bounded_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            cert = Path(directory) / "cert.pem"
            key = Path(directory) / "key.pem"
            subprocess.run([
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-subj", "/CN=display-test", "-days", "1",
                "-keyout", str(key), "-out", str(cert),
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            key.chmod(0o600)
            cert.chmod(0o644)
            self.assertTrue(self.server_module.load_certificate(cert).startswith(b"-----BEGIN CERTIFICATE-----"))
            self.server_module.validate_private_key(key)
            self.assertEqual(self.server_module.validate_tls_material(cert, key), cert.read_bytes())

    def test_certificate_helper_bind_collision_fails_closed(self):
        first = self.server_module.CertificateServer(("127.0.0.1", 0), self.server_module.CertificateHandler)
        try:
            with self.assertRaises(OSError):
                self.server_module.CertificateServer(
                    ("127.0.0.1", first.server_port), self.server_module.CertificateHandler
                )
        finally:
            first.server_close()

    def test_parent_identity_detects_pid_reuse_guard(self):
        identity = self.server_module.process_identity(os.getpid())
        self.assertIsNotNone(identity)
        server = mock.Mock()
        with mock.patch.object(self.server_module, "process_identity", return_value="different"):
            self.server_module._monitor_parent(server, os.getpid(), str(identity))
        server.shutdown.assert_called_once_with()

    def test_launcher_requires_verified_helper_and_trusted_lan_wording(self):
        launcher = (DISPLAY / "start-display.sh").read_text(encoding="utf-8")
        for required in (
            "--validate-only", "--parent-identity", "downloaded_hash", "owned_pid_from_file",
            "write_pid_record", "--connect-timeout 1 --max-time 2", "GM_CERT_PORT",
            "failed bind/readiness/content verification", "trusted LAN only",
            "not safe bootstrap on a hostile LAN",
        ):
            self.assertIn(required, launcher)
        self.assertLess(
            launcher.index('if [[ -L "$DISPLAY_DIR/cert.pem"'),
            launcher.index('if [[ ! -f "$DISPLAY_DIR/cert.pem"'),
        )
        self.assertIn("OpenSSL must support the -addext subjectAltName option", launcher)
        identity_failure = launcher.index("display process exited before its identity could be recorded")
        self.assertLess(launcher.rfind('kill "$APP_PID"', 0, identity_failure), identity_failure)

    def test_launcher_readiness_probe_executes_owned_exact_body_policy(self):
        script = DISPLAY / "start-display.sh"
        harness = r'''
set -e
export GM_DISPLAY_LIB_ONLY=1
source "$1"
owned_pid_from_file() { [[ "${OWNED:-1}" == 1 ]]; }
curl() { printf '%s' "${BODY:-ok}"; }
BODY=ok OWNED=1 probe_owned_display "https://127.0.0.1:5001"
if BODY=pong OWNED=1 probe_owned_display "http://127.0.0.1:5001"; then exit 20; fi
if BODY=ok OWNED=0 probe_owned_display "http://127.0.0.1:5001"; then exit 21; fi
'''
        subprocess.run(["bash", "-c", harness, "probe-test", str(script)], check=True)

    def test_launcher_waits_for_delayed_owned_exit_before_removing_record(self):
        script = DISPLAY / "start-display.sh"
        harness = r'''
set -e
export GM_DISPLAY_LIB_ONLY=1 GM_DISPLAY_STOP_ATTEMPTS=50 GM_DISPLAY_STOP_INTERVAL=0.02
source "$1"
record="$2/owned.pid"
ready="$2/ready"
marker="delayed-owned-process"
python3 -c 'import pathlib,signal,sys,time; signal.signal(signal.SIGTERM, lambda *_: (time.sleep(.18), sys.exit(0))); pathlib.Path(sys.argv[1]).write_text("ready"); time.sleep(20)' "$ready" "$marker" &
child=$!
identity=""
for _ in $(seq 1 50); do [[ -f "$ready" ]] && identity=$(process_identity "$child") && break; sleep .01; done
write_pid_record "$record" "$child" "$identity"
started=$(python3 -c 'import time; print(time.monotonic())')
stop_owned_pid_file "$record" "$marker"
elapsed=$(python3 -c 'import sys,time; print(time.monotonic()-float(sys.argv[1]))' "$started")
wait "$child" 2>/dev/null || true
[[ ! -e "$record" && ! -e "$record.identity" ]]
python3 -c 'import sys; assert float(sys.argv[1]) >= .14' "$elapsed"
'''
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["bash", "-c", harness, str(script), str(script), directory], check=True)

    def test_launcher_stale_identity_never_signals_unrelated_pid(self):
        script = DISPLAY / "start-display.sh"
        harness = r'''
set -e
export GM_DISPLAY_LIB_ONLY=1 GM_DISPLAY_STOP_ATTEMPTS=2 GM_DISPLAY_STOP_INTERVAL=0.01
source "$1"
record="$2/stale.pid"
sleep 20 &
unrelated=$!
printf '%s\n' "$unrelated" > "$record"
printf '%s\n' 'definitely-not-this-process' > "$record.identity"
stop_owned_pid_file "$record" "$2/expected-marker.py"
kill -0 "$unrelated"
[[ ! -e "$record" && ! -e "$record.identity" ]]
kill "$unrelated"
wait "$unrelated" 2>/dev/null || true
'''
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["bash", "-c", harness, str(script), str(script), directory], check=True)

    def test_launcher_boundedly_escalates_an_owned_process_that_does_not_exit(self):
        script = DISPLAY / "start-display.sh"
        harness = r'''
set -e
export GM_DISPLAY_LIB_ONLY=1 GM_DISPLAY_STOP_ATTEMPTS=3 GM_DISPLAY_STOP_INTERVAL=0.02
source "$1"
record="$2/stubborn.pid"
ready="$2/ready"
marker="stubborn-owned-process"
python3 -c 'import pathlib,signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); pathlib.Path(sys.argv[1]).write_text("ready"); time.sleep(20)' "$ready" "$marker" &
child=$!
for _ in $(seq 1 50); do [[ -f "$ready" ]] && identity=$(process_identity "$child") && break; sleep .01; done
write_pid_record "$record" "$child" "$identity"
stop_owned_pid_file "$record" "$marker"
wait "$child" 2>/dev/null || true
if kill -0 "$child" 2>/dev/null; then exit 30; fi
[[ ! -e "$record" && ! -e "$record.identity" ]]
'''
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["bash", "-c", harness, str(script), str(script), directory], check=True)

    def test_explicit_tls_context_fails_closed_and_wraps_valid_material(self):
        app_module = _load_module(DISPLAY / "gm-display-app.py", "tls_context_display_test")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cert = root / "cert.pem"
            key = root / "key.pem"
            with self.assertRaises(Exception):
                app_module._build_tls_context(str(cert), str(key))
            cert.write_text("invalid", encoding="ascii")
            key.write_text("invalid", encoding="ascii")
            key.chmod(0o600)
            with self.assertRaises(Exception):
                app_module._build_tls_context(str(cert), str(key))
            subprocess.run([
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-subj", "/CN=display-test", "-days", "1",
                "-keyout", str(key), "-out", str(cert),
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            key.chmod(0o600)
            cert.chmod(0o644)
            self.assertIsNotNone(app_module._build_tls_context(str(cert), str(key)))


class StagingTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.tmp_path = Path(cls.tmp.name)
        cls.check_input = cls.tmp_path / "check_input.py"
        cls.check_input.write_bytes((DISPLAY / "check_input.py").read_bytes())
        cls.app = _load_module(DISPLAY / "gm-display-app.py", "party_input_display_app")
        cls.app.QUEUE_FILE = str(cls.tmp_path / ".input_queue")
        cls.app.TRIGGER_FILE = str(cls.tmp_path / ".input_trigger")
        cls.app._token_ok = lambda: True
        cls.app._rate_ok = lambda _address: True
        cls.app._device_ok = lambda _device, _address: "approved"
        cls.client = cls.app.app.test_client()

    def setUp(self):
        self.app._staged.clear()
        self.app._input_queue.clear()
        self.app._queue_status.clear()
        self.app._expected_count = 1
        self.app._current_stats = {"players": [{"name": "Mythlon"}]}
        self.player_device = "player-device-0001"
        self.gm_device = "gm-device-0000001"
        with self.app._combat_devices_lock:
            self.app._combat_devices.clear()
        self.player_grant = self.app._authorize_combat_device(
            self.player_device, "player", ["test-campaign"], ["mythlon"]
        )
        self.gm_grant = self.app._authorize_combat_device(self.gm_device, "gm", ["test-campaign"], [])
        Path(self.app.QUEUE_FILE).unlink(missing_ok=True)
        Path(self.app.TRIGGER_FILE).unlink(missing_ok=True)

    def _combat_headers(self, device_id=None):
        selected = device_id or self.player_device
        grant = self.gm_grant if selected == self.gm_device else self.player_grant
        return {
            "X-DND-Combat-Token": grant["capability_token"],
            "X-DND-Device": selected,
        }

    def _stage(self, text: str):
        return self.client.post(
            "/player-input/stage", json={"character": "Mythlon", "text": text}
        )

    def _send(self, text: str, character: str = "Mythlon"):
        return self.client.post(
            "/player-input/send", json={"character": character, "text": text}
        )

    def test_direct_send_creates_immediate_attributed_multiline_trigger(self):
        text = 'I step forward.\n\n"Stay behind me."\nI raise my shield.'
        response = self._send(text)
        self.assertEqual(response.status_code, 204)
        payload = json.loads(Path(self.app.TRIGGER_FILE).read_text(encoding="utf-8"))
        self.assertEqual(payload, [{"character": "Mythlon", "text": text}])
        self.assertFalse(Path(self.app.QUEUE_FILE).exists())

    def test_direct_send_rejects_empty_and_over_limit_input(self):
        empty = self._send(" \n\n ")
        oversized = self._send("x" * (self.app.MAX_PLAYER_INPUT_CHARS + 1))
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(oversized.status_code, 413)
        self.assertFalse(Path(self.app.TRIGGER_FILE).exists())

    def test_direct_send_requires_token_authorization(self):
        with mock.patch.object(self.app, "_token_ok", return_value=False):
            response = self._send("Advance carefully.")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Path(self.app.TRIGGER_FILE).exists())

    def test_direct_send_does_not_mutate_staged_actions(self):
        self.app._staged["Mythlon"] = {
            "text": "Previously staged", "ready": False, "timestamp": 1,
        }
        before = json.loads(json.dumps(self.app._staged))
        response = self._send("Immediate action")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.app._staged, before)

    def test_direct_send_refuses_to_overwrite_existing_trigger(self):
        existing = b'[{"character":"Other","text":"Already pending"}]'
        Path(self.app.TRIGGER_FILE).write_bytes(existing)
        response = self._send("Do not overwrite this")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Path(self.app.TRIGGER_FILE).read_bytes(), existing)

    def test_typed_browser_attack_reaches_executable_dispatcher(self):
        payload = {
            "schema_version": 1, "campaign": "test-campaign", "request_id": "browser-attack-0001",
            "expected_revision": 2, "actor_id": "mythlon", "target_id": "target-1",
            "weapon": {"item_id": "blade", "instance": 1, "equipped_slot": "main_hand"},
            "attack_kind": "main_hand", "attack_profile_id": "blade-profile",
            "roll": {"mode": "supplied", "raw_d20": 12, "advantage": "normal", "source": "browser"},
            "optional_feature_ids": [],
        }
        with mock.patch.object(self.app, "_active_campaign", return_value="test-campaign"), mock.patch.object(
            self.app._combat_ingress, "dispatch_attack", return_value={"committed": True}
        ) as dispatch:
            response = self.client.post(
                "/combat/attack", json=payload, headers=self._combat_headers()
            )
        self.assertEqual(response.status_code, 200)
        dispatch.assert_called_once_with(Path(self.app._SKILL_DIR), payload)

    def test_typed_browser_lifecycle_reaches_executable_dispatcher(self):
        payload = {
            "schema_version": 1, "campaign": "test-campaign", "request_id": "browser-turn-end-0001",
            "expected_revision": 3, "event_type": "end_turn", "actor_id": "mythlon",
        }
        with mock.patch.object(self.app, "_active_campaign", return_value="test-campaign"), mock.patch.object(
            self.app._combat_ingress, "dispatch_lifecycle", return_value={"committed": True}
        ) as dispatch:
            response = self.client.post(
                "/combat/lifecycle", json=payload, headers=self._combat_headers(self.gm_device)
            )
        self.assertEqual(response.status_code, 200)
        dispatch.assert_called_once_with(Path(self.app._SKILL_DIR), payload)

    def test_free_text_mechanical_attack_endpoint_is_rejected(self):
        with mock.patch.object(self.app, "_active_campaign", return_value="test-campaign"):
            response = self.client.post(
                "/combat/attack", json={"campaign": "test-campaign", "text": "I attack the goblin"},
                headers=self._combat_headers(),
            )
        self.assertEqual(response.status_code, 409)
        self.assertIn("free-text mechanical attacks", response.get_json()["error"])

    def test_combat_mutation_requires_csrf_token_even_on_localhost(self):
        with mock.patch.object(self.app, "_active_campaign", return_value="test-campaign"):
            response = self.client.post("/combat/attack", json={"campaign": "test-campaign"})
        self.assertEqual(response.status_code, 403)

    def test_hostile_origin_cannot_read_page_or_combat_routes(self):
        page = self.client.get("/", headers={"Origin": "https://attacker.example"})
        self.assertEqual(page.status_code, 403)
        self.assertNotIn("Access-Control-Allow-Origin", page.headers)
        with mock.patch.object(self.app._combat_ingress, "dispatch_attack") as dispatch:
            response = self.client.post(
                "/combat/attack", json={"campaign": "test-campaign"},
                headers={**self._combat_headers(), "Origin": "https://attacker.example"},
            )
        self.assertEqual(response.status_code, 403)
        dispatch.assert_not_called()
        projection = self.client.get(
            "/combat/projection", headers={**self._combat_headers(), "Origin": "https://attacker.example"}
        )
        self.assertEqual(projection.status_code, 403)

    def test_trusted_origin_is_exact_and_never_wildcard(self):
        origin = next(iter(self.app._trusted_origins))
        response = self.client.get("/", headers={"Origin": origin})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), origin)
        self.assertNotEqual(response.headers.get("Access-Control-Allow-Origin"), "*")
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")

    def test_missing_origin_is_allowed_only_for_loopback_internal_calls(self):
        local = self.client.get("/ping")
        remote = self.client.post(
            "/combat/attack", json={"campaign": "test-campaign"},
            headers=self._combat_headers(), environ_base={"REMOTE_ADDR": "192.0.2.10"},
        )
        self.assertEqual(local.status_code, 200)
        self.assertEqual(remote.status_code, 403)

    def test_remote_combat_uses_direct_transport_and_ignores_forwarded_https(self):
        payload = {
            "schema_version": 1, "campaign": "test-campaign", "request_id": "transport-attack-0001",
            "expected_revision": 2, "actor_id": "mythlon", "target_id": "target-1",
            "weapon": {"item_id": "blade", "instance": 1, "equipped_slot": "main_hand"},
            "attack_kind": "main_hand", "attack_profile_id": "blade-profile",
            "roll": {"mode": "supplied", "raw_d20": 12, "advantage": "normal", "source": "browser"},
            "optional_feature_ids": [],
        }
        origin = next(iter(self.app._trusted_origins))
        headers = {**self._combat_headers(), "Origin": origin, "X-Forwarded-Proto": "https"}
        with mock.patch.object(self.app, "_active_campaign", return_value="test-campaign"), mock.patch.object(
            self.app._combat_ingress, "dispatch_attack", return_value={"committed": True}
        ) as dispatch:
            insecure = self.client.post(
                "/combat/attack", json=payload, headers=headers,
                environ_base={"REMOTE_ADDR": "192.0.2.10", "wsgi.url_scheme": "http"},
            )
            secure = self.client.post(
                "/combat/attack", json=payload, headers=headers,
                environ_base={"REMOTE_ADDR": "192.0.2.10"}, base_url="https://localhost",
            )
        self.assertEqual(insecure.status_code, 403)
        self.assertEqual(secure.status_code, 200)
        dispatch.assert_called_once()

    def test_remote_authorize_lifecycle_and_projection_require_direct_https(self):
        origin = next(iter(self.app._trusted_origins))
        headers = {**self._combat_headers(self.gm_device), "Origin": origin}
        http = {"REMOTE_ADDR": "192.0.2.11", "wsgi.url_scheme": "http"}
        https = {"REMOTE_ADDR": "192.0.2.11"}
        lifecycle = {
            "schema_version": 1, "campaign": "test-campaign", "request_id": "transport-life-0001",
            "expected_revision": 2, "event_type": "end_turn", "actor_id": "mythlon",
        }
        authorization = {
            "device_id": "remote-player-0001", "role": "player",
            "campaigns": ["test-campaign"], "actor_ids": ["mythlon"],
        }
        projection = {"combat_id": "combat-1", "combat_revision": 2, "projection": {}}
        with mock.patch.object(self.app, "_active_campaign", return_value="test-campaign"), mock.patch.object(
            self.app._combat_ingress, "dispatch_lifecycle", return_value={"committed": True}
        ) as dispatch, mock.patch.object(
            self.app._combat_ingress, "_campaign_store", return_value=Path("/tmp/test-combat-state.json")
        ), mock.patch.object(
            self.app._combat_ingress.combat, "read_display_projection", return_value=projection
        ) as read_projection:
            for path, method, body in (
                ("/combat/device/authorize", "post", authorization),
                ("/combat/lifecycle", "post", lifecycle),
                ("/combat/projection", "get", None),
            ):
                insecure = getattr(self.client, method)(path, json=body, headers=headers, environ_base=http)
                secure = getattr(self.client, method)(
                    path, json=body, headers=headers, environ_base=https, base_url="https://localhost"
                )
                self.assertEqual(insecure.status_code, 403, path)
                self.assertEqual(secure.status_code, 200, path)
        dispatch.assert_called_once()
        read_projection.assert_called_once()

    def test_player_authorization_is_actor_and_campaign_scoped(self):
        payload = {
            "schema_version": 1, "campaign": "test-campaign", "request_id": "browser-wrong-actor-0001",
            "expected_revision": 2, "actor_id": "other", "target_id": "target-1",
            "weapon": {"item_id": "blade", "instance": 1, "equipped_slot": "main_hand"},
            "attack_kind": "main_hand", "attack_profile_id": "blade-profile",
            "roll": {"mode": "supplied", "raw_d20": 12, "advantage": "normal", "source": "browser"},
            "optional_feature_ids": [],
        }
        with mock.patch.object(self.app, "_active_campaign", return_value="test-campaign"), mock.patch.object(
            self.app._combat_ingress, "dispatch_attack"
        ) as dispatch:
            response = self.client.post("/combat/attack", json=payload, headers=self._combat_headers())
        self.assertEqual(response.status_code, 403)
        dispatch.assert_not_called()
        with mock.patch.object(self.app, "_active_campaign", return_value="other-campaign"):
            other = self.client.post("/combat/attack", json={**payload, "campaign": "other-campaign"}, headers=self._combat_headers())
        self.assertEqual(other.status_code, 403)

    def test_player_cannot_invoke_lifecycle_and_gm_can(self):
        payload = {
            "schema_version": 1, "campaign": "test-campaign", "request_id": "role-lifecycle-0001",
            "expected_revision": 3, "event_type": "combat_end", "actor_id": None,
        }
        with mock.patch.object(self.app, "_active_campaign", return_value="test-campaign"), mock.patch.object(
            self.app._combat_ingress, "dispatch_lifecycle", return_value={"committed": True}
        ) as dispatch:
            player = self.client.post("/combat/lifecycle", json=payload, headers=self._combat_headers())
            gm = self.client.post("/combat/lifecycle", json=payload, headers=self._combat_headers(self.gm_device))
        self.assertEqual(player.status_code, 403)
        self.assertEqual(gm.status_code, 200)
        dispatch.assert_called_once()

    def test_combat_projection_is_gm_only(self):
        projection = {"combat_id": "combat-1", "combat_revision": 2, "projection": {}}
        with mock.patch.object(self.app, "_active_campaign", return_value="test-campaign"), mock.patch.object(
            self.app._combat_ingress, "_campaign_store", return_value=Path("/tmp/test-combat-state.json")
        ), mock.patch.object(
            self.app._combat_ingress.combat, "read_display_projection", return_value=projection
        ) as read_projection:
            player = self.client.get("/combat/projection", headers=self._combat_headers())
            gm = self.client.get("/combat/projection", headers=self._combat_headers(self.gm_device))
        self.assertEqual(player.status_code, 403)
        self.assertEqual(gm.status_code, 200)
        self.assertEqual(gm.get_json(), projection)
        read_projection.assert_called_once()

    def test_successful_bootstrap_without_active_combat_returns_idle(self):
        device = "idle-local-device-0001"
        with mock.patch.object(self.app, "_active_campaign", return_value="test-campaign"):
            bootstrap = self.client.post(
                "/combat/device/bootstrap",
                json={"device_id": device, "client_mode": "gm-display"},
            )
            grant = bootstrap.get_json()
            with mock.patch.object(
                self.app._combat_ingress, "_campaign_store",
                side_effect=self.app._combat_ingress.NoActiveCombatError(
                    "selected campaign has no authoritative combat store"
                ),
            ):
                projection = self.client.get(
                    "/combat/projection",
                    headers={
                        "X-DND-Device": device,
                        "X-DND-Combat-Token": grant["capability_token"],
                    },
                )
        self.assertEqual(bootstrap.status_code, 200)
        self.assertEqual(projection.status_code, 200)
        self.assertEqual(projection.get_json(), {"status": "idle", "campaign": "test-campaign"})

    def test_unauthorized_projection_is_rejected_before_idle_detection(self):
        with mock.patch.object(self.app, "_active_campaign", return_value="test-campaign"), mock.patch.object(
            self.app._combat_ingress, "_campaign_store"
        ) as campaign_store:
            response = self.client.get("/combat/projection")
        self.assertEqual(response.status_code, 403)
        campaign_store.assert_not_called()

    def test_stale_and_campaign_mismatched_projections_remain_rejected(self):
        failures = (
            (self.app._combat_ingress.combat.DestinationConflictError("stale"), 409, "stale"),
            (self.app._combat_ingress.AttackIngressError("campaign mismatch"), 503, "unavailable"),
        )
        with mock.patch.object(self.app, "_active_campaign", return_value="test-campaign"):
            for failure, status, message in failures:
                with self.subTest(status=status, message=message), mock.patch.object(
                    self.app._combat_ingress, "_campaign_store", side_effect=failure
                ):
                    response = self.client.get(
                        "/combat/projection", headers=self._combat_headers(self.gm_device)
                    )
                self.assertEqual(response.status_code, status)
                self.assertIn(message, response.get_json()["error"])

    def test_symlinked_combat_store_is_not_treated_as_idle(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"GM_CAMPAIGN_ROOT": directory}
        ):
            root = Path(directory)
            campaign = root / "campaigns" / "test-campaign"
            campaign.mkdir(parents=True)
            target = root / "combat-state.json"
            target.write_text("{}", encoding="utf-8")
            (campaign / "combat-state.json").symlink_to(target)
            with self.assertRaises(self.app._combat_ingress.AttackIngressError) as raised:
                self.app._combat_ingress._campaign_store(Path(self.app._SKILL_DIR), "test-campaign")
        self.assertNotIsInstance(raised.exception, self.app._combat_ingress.NoActiveCombatError)

    def test_combat_grants_have_bounded_lifetimes_and_campaign_revocation(self):
        with self.assertRaisesRegex(ValueError, "expiry"):
            self.app._authorize_combat_device(
                "long-player-device", "player", ["test-campaign"], ["mythlon"],
                expires_at=self.app._time.time() + 13 * 3600,
            )
        self.app._revoke_campaign_combat_devices("test-campaign")
        self.assertIsNone(self.app._combat_authorization(self.player_device, "test-campaign"))
        self.assertIsNone(self.app._combat_authorization(self.gm_device, "test-campaign"))

    def test_loopback_bootstrap_returns_stable_per_device_capability(self):
        device = "bootstrap-local-device"
        with mock.patch.object(self.app, "_active_campaign", return_value="test-campaign"):
            body = {"device_id": device, "client_mode": "gm-display"}
            first = self.client.post("/combat/device/bootstrap", json=body)
            second = self.client.post("/combat/device/bootstrap", json=body)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        first_grant = first.get_json()
        second_grant = second.get_json()
        self.assertRegex(first_grant["capability_token"], r"^[0-9a-f]{64}$")
        self.assertEqual(first_grant["capability_token"], second_grant["capability_token"])
        self.assertEqual(first_grant["grant_id"], second_grant["grant_id"])
        with self.app._combat_devices_lock:
            stored = self.app._combat_devices[(device, "test-campaign")]
            self.assertNotIn("capability_token", stored)
            self.assertNotIn(first_grant["capability_token"], json.dumps(stored))
        with self.app.app.test_request_context(headers={"X-DND-Combat-Token": first_grant["capability_token"]}):
            self.assertTrue(self.app._combat_capability_ok(device, "test-campaign"))

    def test_remote_browser_bootstrap_is_denied_with_explicit_claim_guidance(self):
        origin = next(iter(self.app._trusted_origins))
        with mock.patch.object(self.app, "_active_campaign", return_value="test-campaign"):
            response = self.client.post(
                "/combat/device/bootstrap", json={
                    "device_id": "remote-device-0001", "client_mode": "gm-display",
                },
                headers={"Origin": origin}, environ_base={"REMOTE_ADDR": "192.0.2.25"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(response.get_json()["claim_required"])
        self.assertIn("local full display", response.get_json()["error"])

    def test_revocation_and_expiry_rotate_random_grant_and_capability(self):
        device = "rotating-device-0001"
        first = self.app._authorize_combat_device(device, "gm", ["test-campaign"], [])
        self.app._revoke_combat_device(device, "test-campaign")
        after_revoke = self.app._authorize_combat_device(device, "gm", ["test-campaign"], [])
        with self.app._combat_devices_lock:
            self.app._combat_devices[(device, "test-campaign")]["expires_at"] = self.app._time.time() - 1
        after_expiry = self.app._authorize_combat_device(device, "gm", ["test-campaign"], [])
        self.assertEqual(len({first["grant_id"], after_revoke["grant_id"], after_expiry["grant_id"]}), 3)
        self.assertEqual(len({first["capability_token"], after_revoke["capability_token"], after_expiry["capability_token"]}), 3)

    def test_changed_requested_expiry_rotates_grant_but_same_expiry_is_idempotent(self):
        device = "expiry-device-0001"
        now = self.app._time.time()
        first = self.app._authorize_combat_device(
            device, "gm", ["test-campaign"], [], expires_at=now + 1800,
        )
        same = self.app._authorize_combat_device(
            device, "gm", ["test-campaign"], [], expires_at=now + 1800,
        )
        changed = self.app._authorize_combat_device(
            device, "gm", ["test-campaign"], [], expires_at=now + 2400,
        )
        self.assertEqual(first["grant_id"], same["grant_id"])
        self.assertNotEqual(first["grant_id"], changed["grant_id"])

    def test_grants_do_not_cross_device_campaign_or_role_boundaries(self):
        first = self.app._authorize_combat_device("scope-device-0001", "player", ["test-campaign"], ["mythlon"])
        other_device = self.app._authorize_combat_device("scope-device-0002", "player", ["test-campaign"], ["mythlon"])
        other_campaign = self.app._authorize_combat_device("scope-device-0001", "player", ["other-campaign"], ["mythlon"])
        other_role = self.app._authorize_combat_device("scope-device-0001", "gm", ["test-campaign"], [])
        tokens = {grant["capability_token"] for grant in (first, other_device, other_campaign, other_role)}
        self.assertEqual(len(tokens), 4)
        with self.app.app.test_request_context(headers={"X-DND-Combat-Token": first["capability_token"]}):
            self.assertFalse(self.app._combat_capability_ok("scope-device-0002", "test-campaign"))
            self.assertFalse(self.app._combat_capability_ok("scope-device-0001", "other-campaign"))
            self.assertFalse(self.app._combat_capability_ok("scope-device-0001", "test-campaign"))

    def test_revoked_or_merely_approved_device_has_no_combat_role(self):
        self.app._revoke_combat_device(self.player_device, "test-campaign")
        with mock.patch.object(self.app, "_active_campaign", return_value="test-campaign"):
            revoked = self.client.post("/combat/attack", json={"campaign": "test-campaign"}, headers=self._combat_headers())
            generic = self.client.post(
                "/combat/attack", json={"campaign": "test-campaign"},
                headers=self._combat_headers("generic-device-0001"),
            )
        self.assertEqual(revoked.status_code, 403)
        self.assertEqual(generic.status_code, 403)

    def test_combat_capability_is_bound_to_its_device_grant(self):
        payload = {
            "schema_version": 1, "campaign": "test-campaign", "request_id": "wrong-capability-0001",
            "expected_revision": 2, "actor_id": "mythlon", "target_id": "target-1",
            "weapon": {"item_id": "blade", "instance": 1, "equipped_slot": "main_hand"},
            "attack_kind": "main_hand", "attack_profile_id": "blade-profile",
            "roll": {"mode": "supplied", "raw_d20": 12, "advantage": "normal", "source": "browser"},
            "optional_feature_ids": [],
        }
        headers = self._combat_headers()
        headers["X-DND-Combat-Token"] = self.gm_grant["capability_token"]
        with mock.patch.object(self.app, "_active_campaign", return_value="test-campaign"), mock.patch.object(
            self.app._combat_ingress, "dispatch_attack"
        ) as dispatch:
            response = self.client.post("/combat/attack", json=payload, headers=headers)
        self.assertEqual(response.status_code, 403)
        dispatch.assert_not_called()

    def test_oversized_combat_body_rejected_before_json_dispatch(self):
        body = json.dumps({"campaign": "test-campaign", "padding": "x" * (65 * 1024)})
        with mock.patch.object(self.app, "_active_campaign", return_value="test-campaign"), mock.patch.object(
            self.app._combat_ingress, "dispatch_attack"
        ) as dispatch:
            response = self.client.post(
                "/combat/attack", data=body, content_type="application/json", headers=self._combat_headers()
            )
        self.assertEqual(response.status_code, 413)
        dispatch.assert_not_called()

    def test_unknown_length_combat_body_is_rejected_before_parsing(self):
        with mock.patch.object(self.app, "request", mock.Mock(content_length=None)):
            self.assertFalse(self.app._combat_body_allowed())

    def test_long_multi_paragraph_action_stages_without_truncation(self):
        text = "First paragraph: " + ("advance carefully. " * 250)
        text += "\n\nSecond paragraph: \"Hold the line!\"\nThird line with (details) and [notes]."
        response = self._stage(text)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.app._staged["Mythlon"]["text"], text)

    def test_over_limit_action_is_rejected_not_sliced(self):
        text = "x" * (self.app.MAX_PLAYER_INPUT_CHARS + 1)
        response = self._stage(text)
        self.assertEqual(response.status_code, 413)
        self.assertNotIn("Mythlon", self.app._staged)
        self.assertIn("exceeds", response.get_json()["error"])

    def test_empty_action_is_not_staged(self):
        response = self._stage(" \n\n ")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("Mythlon", self.app._staged)

    def test_multiline_text_survives_ready_queue_and_check_input(self):
        text = "I step through the archway.\n\n\"Stay behind me,\" I tell the others.\nI ready my shield."
        self.assertEqual(self._stage(text).status_code, 204)
        ready = self.client.post(
            "/player-input/ready", json={"character": "Mythlon", "ready": True}
        )
        self.assertEqual(ready.status_code, 204)
        queued = json.loads(Path(self.app.QUEUE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(queued, [{"character": "Mythlon", "text": text}])

        env = os.environ.copy()
        env["OTGM_INPUT_QUEUE"] = self.app.QUEUE_FILE
        env["PYTHONPATH"] = os.pathsep.join(
            filter(None, (str(DISPLAY), env.get("PYTHONPATH")))
        )
        result = subprocess.run(
            [sys.executable, str(self.check_input), "--peek-json"],
            capture_output=True, text=True, env=env, check=True,
        )
        captured = json.loads(result.stdout)
        self.assertEqual(captured["entries"][0]["text"], text)
        self.assertEqual(captured["output"], f"[Mythlon]: {text}")

    def test_legacy_direct_route_does_not_silently_truncate(self):
        text = "Opening\n\n" + ("A long action sentence. " * 300)
        with mock.patch.object(self.app, "_persist_input_queue"):
            response = self.client.post(
                "/player-input", json={"character": "Mythlon", "text": text}
            )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.app._input_queue[-1]["text"], text)

    def test_long_multiline_action_reaches_display_payload_unsliced(self):
        text = (
            "Opening line.\n\n"
            + ("Unicode route: café → ruins. " * 300)
            + "Final sentence."
        )
        with (
            mock.patch.object(self.app, "_persist_log"),
            mock.patch.object(self.app, "_persist_tail"),
            mock.patch.object(self.app, "_campaign_timestamp", return_value="[0001-03-17 14:35]"),
            mock.patch.object(self.app, "_broadcast") as broadcast,
        ):
            response = self.client.post(
                "/chunk", json={"action": "Player Action", "text": text}
            )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            broadcast.call_args.args[0],
            {"action": "Player Action", "text": text, "campaign_timestamp": "[0001-03-17 14:35]"},
        )

    def test_legacy_and_timestamped_log_entries_remain_distinct(self):
        legacy = {"action": "Player Action", "text": "Mythlon: Wait here."}
        timestamped = {**legacy, "campaign_timestamp": "[0001-03-17 14:35]"}
        self.app._text_log.clear()
        self.app._text_log.extend((legacy, timestamped))
        self.assertNotIn("campaign_timestamp", self.app._text_log[0])
        self.assertEqual(self.app._text_log[1]["campaign_timestamp"], "[0001-03-17 14:35]")


class PlayerActionQuoteSemanticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.autorun = _load_module(DISPLAY / "autorun_wait.py", "party_input_autorun")

    def visible(self, text):
        return self.autorun._visible_text([{"character": "Mythlon", "text": text}])

    def test_unquoted_action_remains_unquoted(self):
        self.assertEqual(self.visible("We'll pool it for now."), "Mythlon: We'll pool it for now.")

    def test_explicit_dialogue_keeps_one_pair_of_quotes(self):
        self.assertEqual(
            self.visible('"We\'ll pool it for now."'),
            'Mythlon: "We\'ll pool it for now."',
        )

    def test_mixed_action_and_dialogue_preserves_structure_and_multiline(self):
        text = 'Walk over to Sassafras and lower my voice.\n"We should keep the key for now."'
        self.assertEqual(self.visible(text), f"Mythlon: {text}")
        self.assertNotIn('""', self.visible(text))

    def test_autorun_echo_and_promotion_send_exact_quote_semantics(self):
        captured = {
            "entries": [{"character": "Mythlon", "text": 'Approach quietly.\n"Wait here."'}],
            "digest": "abc123",
            "output": "queued",
        }
        completed = mock.Mock(returncode=0, stderr="")
        with mock.patch.object(self.autorun.subprocess, "run", return_value=completed) as run:
            self.assertTrue(self.autorun._echo_and_promote(captured))
        self.assertIn("--promote-digest", run.call_args_list[0].args[0])
        self.assertEqual(run.call_args_list[1].kwargs["input"], 'Mythlon: Approach quietly.\n"Wait here."')


class DisplaySendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.send = _load_module(DISPLAY / "send.py", "party_input_send")

    def test_player_action_bypasses_narration_chunking(self):
        text = "First line\n\n" + ("long action text " * 400)
        self.assertGreater(len(text), self.send.CHUNK_LIMIT)
        self.assertGreater(len(self.send._split_paragraphs(text)), 1)
        self.assertEqual(
            self.send._chunks_for_channel(text, is_action=True, is_sideband=False),
            [text],
        )

    def test_existing_narration_chunker_remains_intact(self):
        text = "n" * (self.send.CHUNK_LIMIT + 25)
        chunks = self.send._split_paragraphs(text)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk) <= self.send.CHUNK_LIMIT for chunk in chunks))

    def test_replay_timestamp_and_legacy_absence_are_forwarded(self):
        for flag, expected in (
            (("--campaign-timestamp", "[0001-03-17 14:35]"), "[0001-03-17 14:35]"),
            (("--no-campaign-timestamp",), None),
        ):
            posted = []
            with mock.patch.object(sys, "argv", ["send.py", "--action", "Player Action", *flag]), mock.patch.object(
                sys, "stdin", io.StringIO("Mythlon: Wait here.")
            ), mock.patch.object(self.send, "_read_token", return_value=""), mock.patch.object(
                self.send, "_post", side_effect=lambda _url, body, _token: posted.append(json.loads(body)) or True
            ):
                self.send.main()
            self.assertEqual(posted[0]["campaign_timestamp"], expected)


class WrapperMultilineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.tmp_path = Path(cls.tmp.name)
        cls.wrapper = _load_module(DISPLAY / "wrapper.py", "party_input_wrapper")
        cls.wrapper.CAMP_FILE = str(cls.tmp_path / ".campaign")
        cls.wrapper.STATS_FILE = str(cls.tmp_path / "stats.json")
        Path(cls.wrapper.CAMP_FILE).write_text("test", encoding="utf-8")
        Path(cls.wrapper.STATS_FILE).write_text(
            json.dumps({"players": [{"name": "Mythlon"}]}), encoding="utf-8"
        )

    def test_wrapper_preserves_multiline_action_exactly(self):
        text = "Paragraph one.\n\nParagraph two with !, (plans), and [signals]."
        raw = json.dumps([{"character": "Mythlon", "text": text}])
        sanitized = self.wrapper._sanitize(raw)
        self.assertIsNotNone(sanitized)
        self.assertEqual(json.loads(sanitized)[0]["text"], text)
        self.assertIn(f"[Mythlon]: {text}", self.wrapper._format_injection(sanitized))

    def test_wrapper_accepts_several_paragraphs_and_rejects_over_limit(self):
        accepted = "p" * 15_000
        accepted_raw = json.dumps([{"character": "Mythlon", "text": accepted}])
        self.assertEqual(json.loads(self.wrapper._sanitize(accepted_raw))[0]["text"], accepted)

        rejected = "p" * (self.wrapper._MAX_ACTION_CHARS + 1)
        rejected_raw = json.dumps([{"character": "Mythlon", "text": rejected}])
        self.assertIsNone(self.wrapper._sanitize(rejected_raw))


if __name__ == "__main__":
    unittest.main()
