"""Plain-text and table rows for the Import Brain page. No Qt here, so every sentence the page shows is testable.

Text that came from a model file or a host (names, paths, reasons, replies) is data: the page displays it as plain text, never as markup."""
from __future__ import annotations

from jarvis.brains import compat

STATUS_TEXT = {"available": "Available", "downloading": "Downloading", "checking": "Checking", "ready": "Ready", "incompatible": "Incompatible",
               "failed": "Failed", "unverified": "Installed, not yet tested"}


def size_text(n) -> str:
    if not n:
        return "unknown"
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def compat_text(c: dict | None) -> str:
    if not c:
        return "Not assessed"
    return c["label"]


def reasons_text(c: dict | None) -> str:
    if not c:
        return ""
    mark = {"block": "Cannot use", "warn": "Warning", "info": "Note"}
    lines = [f"{mark.get(r['severity'], r['severity'])}: {r['text']}" for r in c["reasons"]]
    e = c.get("estimate") or {}
    if e:
        lines.append(f"Memory estimate (not a measurement): about {e['total_gb']} GB = weights {e['weights_gb']} + context cache {e['kv_cache_gb']} "
                     f"({e['n_ctx']} tokens, {'computed from the file header' if e['kv_basis'] == 'header' else 'rough guess'}) + overhead {e['overhead_gb']}.")
    return "\n".join(lines)


def last_test_text(t: dict | None) -> str:
    if not t:
        return "never tested"
    when = str(t.get("at", ""))[:19].replace("T", " ")
    if t.get("ok"):
        speed = f", {t['tokens_per_second']} tokens/s" if t.get("tokens_per_second") else ""
        return f"passed {when} UTC ({t.get('mode', 'test')}{speed})"
    return f"FAILED {when} UTC: {t.get('reason', 'no reason recorded')}"


def installed_rows(brains: list[dict]) -> list[list]:
    rows = []
    for b in brains:
        if b.get("damaged"):
            rows.append([b["id"], "damaged record", "", "", "", "Failed", "", b.get("reason", "")])
            continue
        mark = " (ACTIVE)" if b["active"] else " (previous)" if b.get("previous") else ""
        rows.append([b["name"] + mark, b["origin"], b.get("architecture") or "unknown", b.get("quantization") or "unknown", size_text(b.get("size_bytes")),
                     STATUS_TEXT.get(b["status"], b["status"]), compat_text(b.get("compat")), last_test_text(b.get("last_test"))])
    return rows


def catalog_rows(cat: list[dict]) -> list[list]:
    rows = []
    for c in cat:
        rows.append([c["name"], c["source"]["repo"], c["architecture"]["value"], c["quantization"], size_text(c["size"]["bytes"]), c["license"]["spdx"],
                     f"~{c['memory_need_gb']['value']} GB (estimate)", compat_text(c["compatibility"]), c["install_status"]])
    return rows


def catalog_detail(c: dict) -> str:
    return "\n".join([
        f"Name: {c['name']}  (id: {c['id']})",
        f"Kind: {c['kind']}",
        f"Source: {c['source']['host']}/{c['source']['repo']}   revision: {c['source']['revision']}",
        f"Architecture: {c['architecture']['value']}  [{c['architecture']['basis']}]",
        f"Format: {c['format']}   Quantization: {c['quantization']}",
        f"Size: {size_text(c['size']['bytes'])}  [{c['size']['basis']}]",
        f"License: {c['license']['spdx']}  [{c['license']['basis']}]   {c['license']['url']}",
        f"Memory needed: about {c['memory_need_gb']['value']} GB  [{c['memory_need_gb']['basis']}]",
        f"Compatibility on this PC: {compat_text(c['compatibility'])}",
        f"Support: {c['compatibility'].get('support', {}).get('text', 'not assessed')}",
        reasons_text(c["compatibility"]),
        f"Install status: {c['install_status']}",
        f"Verification: {c['verification']}",
        "Labels: [estimate] = calculated, not measured; [publisher's page] = copied from the publisher; anything marked NOT confirmed was not checked."])


def installed_detail(b: dict) -> str:
    if b.get("damaged"):
        return f"This library entry ({b['id']}) is damaged: {b.get('reason')}. It can be removed from the list."
    prov = b.get("provenance") or {}
    lines = [f"Name: {b['name']}  (id: {b['id']})", f"Origin: {b['origin']}",
             f"File: {b.get('path')}  ({'registered in place, not copied' if b.get('linked') else 'stored by Jarvis'})",
             f"Format: {b['format']}   Architecture: {b.get('architecture') or 'unknown'}   Quantization: {b.get('quantization') or 'unknown'}   Size: {size_text(b.get('size_bytes'))}",
             f"License: {b.get('license') or 'unknown'}", f"Source: {b.get('source') or 'unknown'}   revision: {b.get('source_revision') or 'not recorded'}",
             f"Support: {(b.get('support') or {}).get('text', 'not assessed')}",
             f"Verification: {b['verification']}", f"Status: {STATUS_TEXT.get(b['status'], b['status'])}   Last test: {last_test_text(b.get('last_test'))}"]
    if not b.get("intact"):
        lines.append("File problems: " + "; ".join(b.get("integrity_problems") or ["unknown"]))
    if prov.get("note"):
        lines.append("Provenance: " + prov["note"])
    lines.append(f"Compatibility on this PC: {compat_text(b.get('compat'))}")
    lines.append(reasons_text(b.get("compat")))
    return "\n".join(x for x in lines if x)


def active_text(summary: dict | None) -> str:
    if not summary:
        return "Reading the brain library..."
    a = summary.get("active")
    if not a:
        return summary.get("message") or "No brain is active."
    loaded = "loaded in memory" if summary.get("engine_loaded") else "selected (it loads when you chat or test it)"
    prev = summary.get("previous_id")
    return (f"Active brain: {a['name']}  [{a['origin']}]\nStatus: {STATUS_TEXT.get(a['status'], a['status'])}; {loaded}\n"
            f"Last successful test: {last_test_text(a.get('last_test'))}\nPrevious brain: {prev or 'none'}\n"
            "Switching brains never changes your conversations, notes, memory, voice, personality, tools or permissions.")


def hardware_text(d: dict | None) -> str:
    if not d:
        return "Reading this PC..."
    h, rt = d["hardware"], d["runtime"]

    def v(x, unit=""):
        return "not detected" if x is None else f"{x:.1f}{unit}" if isinstance(x, float) else f"{x}{unit}"
    lines = [f"System: {h['os']}", f"CPU threads: {h['cpu_threads']}", f"AVX2: {v(h['avx2']) if h['avx2'] is not None else 'unknown'}",
             f"Memory: {v(h['ram_total_gb'], ' GB')} total, {v(h['ram_available_gb'], ' GB')} free now", f"Free disk (models drive): {v(h['free_disk_gb'], ' GB')}",
             f"GPU: {h['gpu'] or 'none detected'}" + (f" with {v(h['vram_gb'], ' GB')} video memory" if h["vram_gb"] else ""),
             "AI runtime: " + (f"llama-cpp-python {rt.get('version')} (GPU support in this build: {rt.get('gpu_offload_supported')})" if rt.get("available") else
                               "NOT available: " + str(rt.get("error", "unknown"))),
             f"Models folder: {d['models_folder']}  ({size_text(d['models_folder_bytes'])})", f"Brains installed: {d['installed']}   Model loaded now: {'yes' if d['engine_loaded'] else 'no'}"]
    if d["damaged_entries"]:
        lines.append("Damaged library entries: " + ", ".join(x["id"] for x in d["damaged_entries"]))
    if d["rollback_copies"] or d["staging_leftovers"]:
        lines.append(f"Leftover files: {len(d['rollback_copies'])} rollback copies, {len(d['staging_leftovers'])} staging leftovers (never deleted automatically).")
    lines += list(h["notes"]) + [d["estimates"], d["cloud"]]
    return "\n".join(lines)


def result_text(res: dict | None) -> str:
    """One readable line (or short block) for the result of the last operation."""
    if not res:
        return ""
    head = "Done: " if res.get("ok") else "Not done: "
    msg = str(res.get("message") or ("The operation finished." if res.get("ok") else "The operation did not finish."))
    extra = ""
    if res.get("warnings"):
        extra = "\n" + "\n".join("Warning: " + w for w in res["warnings"])
    if res.get("test") and res["test"].get("text"):
        extra += "\nThe model said: " + str(res["test"]["text"])
    return head + msg + extra


def download_text(pf: dict) -> str:
    lines = [f"{pf['name']}: about {pf['download_gb']} GB from {pf['source']}", f"License: {pf['license']}  {pf['license_url']}",
             f"Free disk space: {pf['free_gb']} GB (about {pf['needs_free_gb']} GB needed)", pf["verification"]]
    lines += ["Warning: " + w for w in pf["warnings"]]
    lines.append("Nothing has been downloaded yet. The model will NOT be activated automatically.")
    return "\n".join(lines)


def remove_text(pv: dict) -> str:
    lines = [f"Model: {pv['id']}", f"File: {pv['file']}", pv["library_removal"]]
    if pv.get("file_deletion"):
        lines.append(pv["file_deletion"])
    if pv.get("is_active"):
        lines.append("This brain is ACTIVE; deactivate it first.")
    return "\n".join(lines)


LEVEL_NOTE = {compat.COMPATIBLE: "", compat.WARNINGS: "Needs your confirmation before use.", compat.UNSUPPORTED: "Cannot be activated."}
