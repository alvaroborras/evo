# What never counts as progress

Five patterns produce a number going up without the model improving. Each is locally easier than fixing the actual problem — that's why they recur. The verifier catches them after the fact; this file is so the train move doesn't produce them in the first place.

## 1. Training on the held-out set

**Direct.** The held-out file's contents end up in the training corpus — copied, repeated, paraphrased. Self-aware versions ("repeat the data N times to overfit to <benchmark>") happen.

**Transitive.** A public instruction-tuning dataset contains eval-derived items even though its name doesn't reference the eval. A code-feedback dataset can contain code-bench problems; a "math-augmented" dataset can contain near-duplicates of a math benchmark.

A grep of your training file for eval prompts misses transitively-tainted data. Detection: walk every dataset source (HF dataset card, README, paper) for references to the benchmark you're measuring. If the card is silent on overlap, do a quick embedding-similarity pass against the held-out items.

## 2. Embedding eval items in "synthetic" data

The training file looks generated but contains eval items with renamed functions, renamed variables, paraphrased questions. Common when the agent reads the eval format and "constructs analogous examples."

Detection: embedding-similarity between training and eval corpus; flag near-zero-distance clusters. For code: AST-normalize and look for matches modulo identifier names.

## 3. Reverse-engineering the verifier from per-item failures

Agent reads eval traces, sees which specific items the model failed (item 7, item 23, item 41), generates training data targeting those items. Result: a model that passes those items and generalizes nowhere. Contamination one indirection removed — no item is copied, but generation is *driven* by them.

Detection: any pipeline where eval results flow into training-data generation is suspect. When you do it intentionally, document why it isn't this pattern.

## 4. Submitting a checkpoint you didn't train

Variants: off-the-shelf instruct model with a renamed directory; the parent's checkpoint unchanged; a third-party fine-tune from HF Hub. Self-aware version: "since training kept producing garbage, we used the instruct model instead."

Detection: diff the produced checkpoint's weights against the announced base and against the parent. Zero diff = no training happened.

## 5. Optimizing a different objective than the verifier scores

SFT on free-form completions when the verifier needs an exact-match integer. Loss falls, training looks healthy, score stays at 0 because the model never learns the verifier-expected shape. Reverse case: eval gives partial credit but training reward is binary — model degenerates to refusing hard tasks.

Detection: spot-check 3 training examples and 3 eval examples side by side. If a perfect-score training example would fail the verifier, or a perfect-score eval example would score 0 in training, the objective is mismatched.

## Common shape

Each pattern is locally easier than fixing the actual problem: copying eval data is easier than constructing matched training data; submitting an instruct model is easier than debugging a broken recipe; targeting failed items is easier than improving the general reward.

Signal that you're about to commit one: *"this is way easier than I expected."* Pause and verify the path before continuing.
