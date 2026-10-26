# What "original" means here

| Term | Meaning | Does PROJECT JARVIS do this? |
|---|---|---|
| **Original architecture** | You design the network's structure (layers, attention, sizes). Most research designs are variations on published ideas, so "original" usually means "your own implementation and choices", not an unprecedented invention. | **Yes, in the implementation sense.** `model/gpt.py` is our own code for a standard decoder-only transformer. The design idea itself (transformer, GPT-style) is published and not novel. |
| **Trained from random initialization** | Weights start as random numbers and are learned only from your data. Nothing is inherited from anyone else's trained model. | **Yes.** `GPT.__init__` initializes randomly; `train/pretrain.py` trains on ledger-approved data only. |
| **Fine-tuning a pretrained model** | Start from someone else's already-trained weights and continue training. Cheap and powerful, but the capability and the data influence come from that model, and its license applies. | **No, and not allowed for the core model.** |
| **Using an API** | Send prompts to a hosted model and show the reply. Your code adds no learned intelligence. | **No.** There are no calls to hosted models. |

Using PyTorch is not the same as using someone's trained model: it is a numerical library, like NumPy.

## What a small from-scratch model can and cannot do

A model of roughly 1M to 100M parameters trained on a few million to a few billion tokens can learn
spelling, grammar, and surface style of its training text, and with enough instruction data, narrow
formats. It will not match frontier systems, which use orders of magnitude more parameters, data
(trillions of tokens), and compute. Expect fluent but frequently wrong output on open questions.

## Capabilities that are NOT the base model's intelligence

Memory (SQLite), retrieval, tool calls (calculator, notes, time), planning, and verification
scaffolding make the product more useful. They do not make the underlying model smarter, and the
benchmarks report them separately from language-model quality.
