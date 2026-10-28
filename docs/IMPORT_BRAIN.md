# Import Brain (Project JARVIS)

Open **Import Brain** in the left list. Everything happens on this PC; Jarvis never switches to a cloud model or paid service on its own.

* **Import Model**: pick a `.gguf` file. *Check file* only reads its header and tells you what it is and whether this PC can run it. *Import* adds it to your library; by default it stays where it is (not copied). Importing does not activate it.
* **Browse Models**: models Jarvis can download. *Download...* shows size, license, free disk space and warnings first; nothing downloads until you confirm, and the model is not activated automatically.
* **Installed Brains**: *Test* loads the model and makes it answer a real question. *Activate* tests it first and switches only if it answers; otherwise your current brain stays (or is put back). *Remove...* asks first and tells you whether your own file would be deleted.
* **Active Brain**: shows which brain is active, its last test, and lets you deactivate it or go back to the previous one. Switching never changes your conversations, notes, memory, voice, personality, tools or permissions.
* **Support level**: *Verified on this PC* appears only after a real test succeeded here. *Expected, not verified* means the runtime knows the architecture and tokenizer, nothing more. Adapters, vision projectors, split (multi-part) models, vocabulary-only files and files with a tokenizer the runtime does not know are *Unsupported*. DeepSeek: distilled R1 models are ordinary Qwen/Llama files; the full V2/V3/R1 files need hundreds of GB; none has been tested.
* **Compatibility**: *Compatible*, *Compatible with Warnings* (needs your confirmation) or *Unsupported* (cannot be activated). The memory figures are estimates, not measurements.
* **Imported files are untrusted**: Jarvis reads their header only and never runs anything inside them. An imported file has no trusted checksum to compare with, so it is shown as unverified.

Details and limits: `docs/STAGE5_MODEL_SOURCE_VERIFICATION.md` sections 28 and 29 (final audit).
