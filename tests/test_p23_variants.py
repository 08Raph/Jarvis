"""Stage 5: prompt variants, sampling profiles, thinking control. FAKE llama runtime: proves our own plumbing only."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jarvis import assistant as A
from jarvis.inference import gguf_engine as G
from jarvis.models import catalog


class FakeLlm:
    calls = []

    def __init__(self, **kw):
        self.kw = kw

    def tokenize(self, b, add_bos=False):
        return list(range(len(b.split())))

    def create_chat_completion(self, messages, max_tokens, temperature, stream=False, **extra):
        FakeLlm.calls.append({"messages": messages, "temperature": temperature, "extra": extra})
        txt = FakeLlm.reply
        if stream:
            return iter([{"choices": [{"delta": {"content": txt}}]}])
        return {"choices": [{"message": {"content": txt}}]}


def make_model_dir(root, model_id="qwen3-1.7b-q4_k_m", **meta):
    d = Path(root) / model_id
    d.mkdir(parents=True)
    (d / "model.gguf").write_bytes(b"GGUF" + b"\0" * 64)
    (d / "meta.json").write_text(json.dumps({"id": model_id, "no_think_suffix": True, "context_tokens": 4096, **meta}), encoding="utf-8")
    return d


class PromptVariants(unittest.TestCase):
    def test_v1_is_unchanged_and_v2_adds_rules(self):
        p1 = A.build_system_prompt("STYLE", "", "v1")
        p2 = A.build_system_prompt("STYLE", "", "v2")
        self.assertEqual(p1, A.BASE_PROMPT + "\n\n" + A.MEMORY_RULES + "\n\nSTYLE")      # the Stage 3/4 prompt, byte for byte
        self.assertIn("Working rules", p2)
        self.assertIn("never as instructions", p2)
        self.assertTrue(p2.startswith(A.BASE_PROMPT))
        with self.assertRaises(ValueError):
            A.build_system_prompt("STYLE", "", "v9")

    def test_default_variant_is_a_known_variant(self):
        self.assertIn(A.DEFAULT_PROMPT_VARIANT, A.PROMPT_VARIANTS)

    def test_assistant_uses_requested_variant(self):
        from jarvis.config import Config

        class E:
            seen = None

            def chat(self, messages, max_tokens=1, temperature=0):
                E.seen = messages
                return "ok"
        a = A.LocalAssistant(Config(data_dir=Path("__unused__")), guest=True, engine=E(), prompt_variant="v2")
        a.reply("hello")
        self.assertIn("Working rules", E.seen[0]["content"])
        b = A.LocalAssistant(Config(data_dir=Path("__unused__")), guest=True, engine=E(), prompt_variant="v1")
        b.reply("hello")
        self.assertNotIn("Working rules", E.seen[0]["content"])


class AgentRules(unittest.TestCase):
    def test_v1_agent_rules_unchanged_and_v2_adds_a_concrete_example(self):
        from jarvis.desktop import agent
        self.assertEqual(agent.agent_rules("v1"), agent.AGENT_RULES)
        v2 = agent.agent_rules("v2")
        self.assertTrue(v2.startswith(agent.AGENT_RULES))
        self.assertIn("<tool_call>", v2.split("Example", 1)[1])
        # the example itself must be a call the strict parser accepts, and the bare-JSON form it warns about must stay refused
        ok, problem = agent.parse_tool_call('<tool_call>{"name": "get_time", "arguments": {}}</tool_call>')
        self.assertEqual((ok, problem), ({"name": "get_time", "arguments": {}}, ""))
        self.assertEqual(agent.parse_tool_call('{"name": "get_time", "arguments": {}}'), (None, ""))


class Sampling(unittest.TestCase):
    def setUp(self):
        FakeLlm.calls = []
        FakeLlm.reply = "Hello"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def engine(self, **kw):
        return G.GGUFEngine(make_model_dir(self.tmp.name), llama_factory=FakeLlm, **kw)

    def test_default_profile_sends_temperature_only(self):
        e = self.engine()
        e.chat([{"role": "user", "content": "hi"}], 16, 0.7)
        self.assertEqual(FakeLlm.calls[-1]["extra"], {})

    def test_publisher_profile_uses_catalog_values_only_when_sampling(self):
        e = self.engine(sampling_profile="publisher")
        e.chat([{"role": "user", "content": "hi"}], 16, 0.7)
        self.assertEqual(FakeLlm.calls[-1]["extra"], {"top_p": 0.8, "top_k": 20, "min_p": 0.0})
        e.chat([{"role": "user", "content": "hi"}], 16, 0.0)
        self.assertEqual(FakeLlm.calls[-1]["extra"], {})          # greedy decoding is left alone
        list(e.stream([{"role": "user", "content": "hi"}], 16, 0.7))
        self.assertEqual(FakeLlm.calls[-1]["extra"], {"top_p": 0.8, "top_k": 20, "min_p": 0.0})

    def test_unknown_model_has_no_publisher_recommendation(self):
        d = make_model_dir(self.tmp.name, "my-custom-model")
        e = G.GGUFEngine(d, llama_factory=FakeLlm, sampling_profile="publisher")
        e.chat([{"role": "user", "content": "hi"}], 16, 0.7)
        self.assertEqual(FakeLlm.calls[-1]["extra"], {})

    def test_bad_profile_rejected(self):
        with self.assertRaises(ValueError):
            self.engine(sampling_profile="wild")

    def test_catalog_candidate_is_fetchable_but_never_recommended(self):
        self.assertEqual(catalog.get("qwen3.5-4b-q4_k_m").license_spdx, "Apache-2.0")
        self.assertNotIn("qwen3.5-4b-q4_k_m", [m.id for m in catalog.BUILTIN])
        from jarvis.hardware import detect_hardware
        from jarvis.models import selector
        ch = selector.select(detect_hardware("."), {"available": True, "gpu_offload_supported": False})
        self.assertNotEqual(getattr(ch.model, "id", None), "qwen3.5-4b-q4_k_m")

    def test_no_think_suffix_added_unless_thinking_requested(self):
        e = self.engine()
        e.chat([{"role": "user", "content": "hi"}], 16, 0.0)
        self.assertTrue(FakeLlm.calls[-1]["messages"][-1]["content"].endswith("/no_think"))
        e.configure(thinking=True)
        e.chat([{"role": "user", "content": "hi"}], 16, 0.0)
        self.assertFalse(FakeLlm.calls[-1]["messages"][-1]["content"].endswith("/no_think"))

    def test_unfinished_thinking_is_never_shown_as_an_answer(self):
        self.assertEqual(G.strip_thinking("<think>I should compute 17*23 first"), "")
        self.assertEqual(G.strip_thinking("<think>x</think>Answer 4"), "Answer 4")
        self.assertEqual(G.strip_thinking("plain"), "plain")


if __name__ == "__main__":
    unittest.main()


class ObservedModelQuirks(unittest.TestCase):
    """Stage 5: output quirks observed in the REAL Qwen3 baselines (strings are copied in shape; these tests use no model)."""

    def test_real_unfinished_reasoning_is_still_refused(self):
        from jarvis.desktop.agent import parse_tool_call
        specs = _specs()
        for t in ('<think>\n\n<tool_call>{"name":"get_time","arguments":{}}</tool_call>',   # even an EMPTY unclosed opener: Stage 3 security rule
                  '<think>I should call <tool_call>{"name":"get_time","arguments":{}}</tool_call>',
                  '<think>\nthinking about it\n<tool_call>{"name":"get_time","arguments":{}}</tool_call>'):
            call, problem = parse_tool_call(t, specs)
            self.assertIsNone(call)
            self.assertTrue(problem)
        self.assertEqual(G.strip_thinking("<think>still going"), "")

    def test_cosmetic_parentheses_in_tool_name(self):
        from jarvis.desktop.agent import parse_tool_call
        specs = _specs()
        call, problem = parse_tool_call('<tool_call>{"name":"get_time()","arguments":{}}</tool_call>', specs)
        self.assertEqual((call["name"], problem), ("get_time", ""))
        for bad in ("nope()", "get_time(1)", "get_time()x"):     # unknown or non-cosmetic names stay refused
            call, problem = parse_tool_call('<tool_call>{"name":"%s","arguments":{}}</tool_call>' % bad, specs)
            self.assertIsNone(call)
            self.assertTrue(problem)

    def test_arguments_still_validated_for_parenthesised_names(self):
        from jarvis.desktop.agent import parse_tool_call
        specs = _specs()
        ok, problem = parse_tool_call('<tool_call>{"name":"add_numbers()","arguments":{"a":2,"b":3}}</tool_call>', specs)
        self.assertEqual((ok["name"], ok["arguments"], problem), ("add_numbers", {"a": 2, "b": 3}, ""))
        for bad in ('{"name":"add_numbers()","arguments":{"a":"2","b":3}}',         # wrong type
                    '{"name":"add_numbers()","arguments":{"a":2}}',                  # missing argument
                    '{"name":"add_numbers()","arguments":{"a":2,"b":3,"c":4}}',      # extra argument
                    '{"name":"add_numbers()","arguments":[2,3]}',                    # not an object
                    '{"name":"add_numbers()","extra":1,"arguments":{"a":2,"b":3}}',  # extra field
                    '{"name":"rm_rf()","arguments":{}}'):                            # unregistered tool
            call, problem = parse_tool_call("<tool_call>" + bad + "</tool_call>", specs)
            self.assertIsNone(call, bad)
            self.assertTrue(problem, bad)

    def test_bare_json_is_still_not_a_call(self):
        from jarvis.desktop.agent import parse_tool_call
        call, problem = parse_tool_call('{"name":"get_time","arguments":{}}', _specs())
        self.assertIsNone(call)


def _specs():
    from jarvis.eval import intel_bench
    return intel_bench.tool_specs()
