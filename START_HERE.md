# Start Cortex v1

For a GitHub checkout, start with [publication setup and evidence boundaries](docs/github_publication.md).
The application can run directly from source; private checkpoints and prebuilt
wheels are not included. Public hosting has not been deployed.

For installation outside this checkout, use the 0.2.0 wheel and
[the submission setup guide](docs/public_submission_v1.md). It includes persistence,
scheduler startup and the exact missing hosting setup. Localhost is not a public tryout link.

For the browser product, run `python -m cortex web` from this folder and open the
owner URL printed in PowerShell. See [browser setup, scheduler and demo](docs/browser_product_v1.md).
Supplier mode works with zero model calls; AI drafts require a configured running provider.
The original command-line supplier tutorial follows unchanged.

1. Extract `Cortex_v1.zip`.
2. Open PowerShell inside the extracted `cortex-v1` folder.
3. Check Python: `python --version` (3.10 or newer).
4. Run: `python -m cortex examples/supplier_task.json`.

You should see supplier **Beta**, total **11500**, and four traced worker actions:
extract, calculate, verify, recommend.

Then run:

```powershell
python -m cortex examples/supplier_task.json --fault bad_arithmetic
```

Look for `verify` failing, `repair_calculation` running, a second verification,
and the same correct final supplier. This is your first experiment: recovery
must add work while preserving correctness.

Next compare:

```powershell
python -m cortex examples/csv_task.json --policy fixed
python -m cortex examples/csv_task.json --policy local
```

The fixed baseline stops; the recovery policy adds a CSV-aware extraction step.
Both are ordinary Python policies, not trained AI. Their traces make the
execution rules easy to inspect before adding live model uncertainty.

The extraction checkpoint adds one focused concept: the governor decides when
to extract, while a separate router chooses the parser or model worker. Python
still checks the extracted facts and calculates the result.

With Ollama running and your installed model available:

```powershell
python -m cortex examples/plain_text_quotes.json --policy local --recovery-mode failure-aware --extraction-mode parser-first --extractor-model qwen2.5:7b-instruct --max-model-calls 1
```

This clear-text development fixture completed as Beta/11500 in the recorded
7B run, but other model extractions failed. The verifier requests clarification
when proposed fields or excerpts cannot be supported. One successful fixture
does not guarantee your next generation will match it.

Read `README.md` for configuration and `docs/heterogeneous_extraction_v1.md` for
the measured comparison and preserved failures. Held-out evaluation, training
and broader scope remain paused for review.
