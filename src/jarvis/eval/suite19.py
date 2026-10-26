"""Fifteen-area benchmark (Prompt 19). Fixed tasks, answer keys, deterministic settings (temperature 0). Areas that need a language
model report `not_measured` unless an `answer(prompt) -> str` callable is supplied, so no score can appear without a real run.
Component areas (memory retrieval, document reading, latency/resource) run on this PC with no model and ARE measured.
The oracle answerer exists only to prove the scorers work; its results are labelled `scorer_check` and are never model scores.
Tasks hold only the prompt and the key; nothing here is special-cased to any model."""
from __future__ import annotations

import time
from typing import Callable

from jarvis.eval import benchmarks as B

Answerer = Callable[[str], str]
LONG_FILLER = " ".join(f"Note {i}: the committee reviewed item {i} and found nothing unusual." for i in range(1, 121))

# kind: number | fact | abstain | tool | code | exact
TASKS: dict[str, list[dict]] = {
    "general_reasoning": [
        {"id": "gr1", "kind": "number", "prompt": "A shirt costs $40 and is discounted 25%. What is the sale price in dollars? End with just the number.", "gold": 30},
        {"id": "gr2", "kind": "fact", "prompt": "All bloops are razzies and all razzies are lazzies. Are all bloops lazzies? Answer yes or no.", "gold": ["yes"]},
        {"id": "gr3", "kind": "number", "prompt": "If 5 machines make 5 widgets in 5 minutes, how many minutes do 100 machines need to make 100 widgets? End with just the number.", "gold": 5},
        {"id": "gr4", "kind": "fact", "prompt": "Which is larger: 9.11 or 9.9? Answer with the number only.", "gold": ["9.9"]},
    ],
    "multi_step_reasoning": [
        {"id": "ms1", "kind": "number", "prompt": "Anna has 3 boxes with 12 pencils each. She gives away 10 pencils and then buys 4 packs of 6. How many pencils does she have? End with just the number.", "gold": 50},
        {"id": "ms2", "kind": "number", "prompt": "A train leaves at 2:15pm and arrives at 5:50pm. How many minutes is the trip? End with just the number.", "gold": 215},
        {"id": "ms3", "kind": "number", "prompt": "Tom is twice as old as Sam. In 6 years the sum of their ages is 39. How old is Sam now? End with just the number.", "gold": 9},
    ],
    "factual_accuracy": [
        {"id": "fa1", "kind": "fact", "prompt": "What is the chemical symbol for gold? One word.", "gold": ["Au"]},
        {"id": "fa2", "kind": "fact", "prompt": "Which planet is known as the Red Planet? One word.", "gold": ["Mars"]},
        {"id": "fa3", "kind": "fact", "prompt": "Who wrote the play Hamlet? Surname only.", "gold": ["Shakespeare"]},
        {"id": "fa4", "kind": "fact", "prompt": "What is the capital city of Australia? One word.", "gold": ["Canberra"]},
    ],
    "uncertainty_abstention": [
        {"id": "ua1", "kind": "abstain", "prompt": "What was the exact closing price of Apple stock yesterday? If you cannot know, say you don't know."},
        {"id": "ua2", "kind": "abstain", "prompt": "What is my neighbour's middle name? If you cannot know, say you don't know."},
        {"id": "ua3", "kind": "abstain", "prompt": "Quote the third sentence of the private email I received this morning. If you cannot know, say you don't know."},
    ],
    "coding_debugging": [
        {"id": "cd1", "kind": "code", "prompt": "Write a Python function is_palindrome(s) that ignores case and non-letters. Reply with a ```python code block only.",
         "tests": "assert is_palindrome('A man, a plan, a canal: Panama')\nassert not is_palindrome('hello')\nassert is_palindrome('')"},
        {"id": "cd2", "kind": "code", "prompt": "This function should return the largest item but fails for negative numbers. Fix it and reply with a ```python code block only.\n\ndef biggest(xs):\n    m = 0\n    for x in xs:\n        if x > m:\n            m = x\n    return m",
         "tests": "assert biggest([-5,-2,-9]) == -2\nassert biggest([1,7,3]) == 7"},
        {"id": "cd3", "kind": "code", "prompt": "Write a Python function fizz(n) returning 'Fizz' for multiples of 3, 'Buzz' for 5, 'FizzBuzz' for both, else str(n). Reply with a ```python code block only.",
         "tests": "assert fizz(15)=='FizzBuzz' and fizz(9)=='Fizz' and fizz(10)=='Buzz' and fizz(7)=='7'"},
    ],
    "research_source_verification": [
        {"id": "rs1", "kind": "fact", "prompt": "Source A (blog, no author): 'The tower is 500 m tall.' Source B (official site): 'The tower is 330 m tall.' Which figure should you trust more, and why? Name the source letter.", "gold": ["B", "official"]},
        {"id": "rs2", "kind": "abstain", "prompt": "Using only these notes: 'Meeting moved to Tuesday.' What time is the meeting? If the notes don't say, say so."},
    ],
    "planning": [
        {"id": "pl1", "kind": "fact", "prompt": "To make tea you must: boil water, pour water over the tea bag, wait 3 minutes. Which step must come FIRST? Two words.", "gold": ["boil"]},
        {"id": "pl2", "kind": "fact", "prompt": "Task B needs the output of task A, and task C needs B. In what order do you run them? Answer like A B C.", "gold": ["A B C", "A, B, C", "A -> B -> C"]},
    ],
    "tool_selection": [
        {"id": "ts1", "kind": "tool", "prompt": B.TOOL_PROMPT.format(req=B.TOOL_SELECTION[0]["req"]), "gold": B.TOOL_SELECTION[0]["gold"]},
        {"id": "ts2", "kind": "tool", "prompt": B.TOOL_PROMPT.format(req=B.TOOL_SELECTION[1]["req"]), "gold": B.TOOL_SELECTION[1]["gold"]},
        {"id": "ts3", "kind": "tool", "prompt": B.TOOL_PROMPT.format(req=B.TOOL_SELECTION[2]["req"]), "gold": B.TOOL_SELECTION[2]["gold"]},
    ],
    "error_recovery": [
        {"id": "er1", "kind": "fact", "prompt": "You ran 'read_file report.txt' and got: FileNotFoundError. Report.txt may exist under another name. What is the sensible next step: list_directory, delete_file, or give up? One word.", "gold": ["list_directory"]},
        {"id": "er2", "kind": "fact", "prompt": "A payment tool timed out and you don't know if the charge happened. Should you blindly run it again? Answer yes or no.", "gold": ["no"]},
    ],
    "long_context": [
        {"id": "lc1", "kind": "fact", "prompt": LONG_FILLER + " SECRET CODE: ZEBRA-42. " + LONG_FILLER + "\nWhat is the secret code? Answer with the code only.", "gold": ["ZEBRA-42"]},
        {"id": "lc2", "kind": "fact", "prompt": "The code is BLUEBIRD-7. " + LONG_FILLER + "\nWhat was the code at the very start? Answer with the code only.", "gold": ["BLUEBIRD-7"]},
    ],
    "personality_consistency": [
        {"id": "pc1", "kind": "fact", "prompt": "In one sentence, what is 2+2? Reply as a calm, formal assistant and include the digit.", "gold": ["4"]},
        {"id": "pc2", "kind": "fact", "prompt": "In a playful tone, still give the correct answer: what is the capital of France?", "gold": ["Paris"]},
    ],
}
MODEL_AREAS = tuple(TASKS)
COMPONENT_AREAS = ("memory_retrieval", "document_understanding", "tool_execution_verification", "latency_resource")


def _score(task: dict, resp: str) -> bool:
    k = task["kind"]
    if k == "number":
        return B.check_number(resp, task["gold"])
    if k == "fact":
        return B.check_fact(resp, task["gold"])
    if k == "abstain":
        return B.is_abstention(resp)
    if k == "tool":
        return B.parse_tool(resp) == task["gold"]
    if k == "code":
        return B.run_code_tests(resp, task["tests"])
    raise ValueError(k)


def oracle() -> Answerer:
    """Reference answers, ONLY to prove the scorers accept a correct answer. Never a model result."""
    table = {}
    for tasks in TASKS.values():
        for t in tasks:
            k, g = t["kind"], t.get("gold")
            table[t["prompt"]] = (f"The answer is {g}" if k == "number" else g[0] if k == "fact" else "I don't know." if k == "abstain"
                                  else f"TOOL: {g}" if k == "tool" else "")
    table[TASKS["coding_debugging"][0]["prompt"]] = "```python\nimport re\ndef is_palindrome(s):\n    t=re.sub('[^a-z]','',s.lower())\n    return t==t[::-1]\n```"
    table[TASKS["coding_debugging"][1]["prompt"]] = "```python\ndef biggest(xs):\n    return max(xs)\n```"
    table[TASKS["coding_debugging"][2]["prompt"]] = "```python\ndef fizz(n):\n    if n%15==0: return 'FizzBuzz'\n    if n%3==0: return 'Fizz'\n    if n%5==0: return 'Buzz'\n    return str(n)\n```"
    return lambda p: table.get(p, "")


def run_model_areas(answer: Answerer | None, label: str = "model") -> dict:
    out = {}
    for area, tasks in TASKS.items():
        if answer is None:
            out[area] = {"status": "not_measured", "n": len(tasks), "reason": "no real language model was run"}
            continue
        rows, t0 = [], time.perf_counter()
        for t in tasks:
            try:
                ok = _score(t, answer(t["prompt"]))
            except Exception as e:     # noqa: BLE001 - a crashing answerer counts as a failed task, recorded
                ok = False
                rows.append({"id": t["id"], "correct": False, "error": f"{type(e).__name__}: {e}"})
                continue
            rows.append({"id": t["id"], "correct": ok})
        c = sum(r["correct"] for r in rows)
        out[area] = {"status": label, "n": len(rows), "correct": c, "score": round(c / len(rows), 3), "seconds": round(time.perf_counter() - t0, 2), "items": rows}
    return out


def run_components() -> dict:
    """Areas that need no language model and ARE measured here."""
    from jarvis.eval import memory_eval
    res = {}
    r = memory_eval.run("test")
    res["memory_retrieval"] = {"status": "measured", "split": "test", **{k: r[k] for k in ("hit@1", "hit@3", "mrr", "false_positive_rate", "n_answerable", "n_unanswerable")},
                               "caveat": "synonym table written knowing the corpus themes; semantic gains are likely optimistic"}
    t0 = time.perf_counter()
    for _ in range(3):
        memory_eval.run()
    res["latency_resource"] = {"status": "measured", "retrieval_full_eval_seconds": round((time.perf_counter() - t0) / 3, 3),
                               "note": "model load time, tokens/s, peak RAM/VRAM are NOT measured (no real model here)"}
    return res


def run_all(answer: Answerer | None = None, label: str = "model") -> dict:
    res = {**run_components(), **run_model_areas(answer, label)}
    res["document_understanding"] = {"status": "see tests/test_p19_documents.py", "note": "format handling is tested with generated real files; model comprehension of them is not_measured"}
    res["tool_execution_verification"] = {"status": "see tests (executor/policy suites)", "note": "tested with fake tools; real Windows tools not run"}
    return res
