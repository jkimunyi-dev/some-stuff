# Out-of-rules experiments

This branch exists to measure what the competition rules cost us. **Nothing from this branch may be used in a Zindi submission.**

| ID | Rule broken | What it tests |
|---|---|---|
| X1 | Model over 3B (qwen2.5:7b) | Is model size the bottleneck? |
| X2 | Learned adaptation on Test material: teacher-labelled examples from judged Test questions shown in the plan prompt | Does teaching from judge verdicts generalise to unseen questions? |
| X3 | Fine-tuning on Test-derived labels (LoRA SFT, then RL) | Does training the student on the teacher's corrections generalise? |

Evaluation discipline: teach only on batches 1-2 and the 150-question sample minus batch-3 IDs; score only on batch 3 and later batches.
