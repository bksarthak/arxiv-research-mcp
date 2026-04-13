# Example user prompts

Natural-language prompts you can paste into Claude Desktop, Claude Code, Cursor, or any MCP client connected to `arxiv-research-mcp`. They assume the server is installed and wired into your client — see the top-level [`README.md`](../README.md) for setup.

Each section assumes you've loaded the matching config from [`examples/topics/`](topics/). Swap configs (or run multiple instances of the server against different configs) to change what the pipeline surfaces.

---

## Table of contents

- [General patterns](#general-patterns)
- [AI × security](#ai--security)
- [Machine learning research](#machine-learning-research)
- [Cryptography](#cryptography)
- [Computational biology](#computational-biology)
- [Distributed systems](#distributed-systems)
- [Debugging and tuning](#debugging-and-tuning)

---

## General patterns

These work regardless of which topic config you've loaded.

**Run the built-in weekly workflow**

> Use the `weekly_digest_workflow` prompt from arxiv-research-mcp to build me a research digest for the last 7 days.

**Ask for a specific window**

> Fetch candidate papers from the last 14 days via arxiv-research-mcp, then apply the `research_judge_rubric` and show me papers that clear relevance ≥ 7 and quality ≥ 7.

**Inspect the cursor before running**

> First read the `cursor://state` resource from arxiv-research-mcp so I can see what's already been surfaced, then build this week's digest.

**Reset the cursor when retuning**

> Call `clear_cursor(confirm=True)` on arxiv-research-mcp, then fetch the last 14 days of candidates so I can re-evaluate everything against my updated `rubric_focus`.

**Bypass the verdict cache for a fresh re-judge**

> Call `fetch_candidate_papers(window_days=7, use_cache=False)` on arxiv-research-mcp, then apply the `research_judge_rubric` to ALL candidates. Submit the verdicts after judging.

**Clear the verdict cache when retuning the rubric**

> Call `clear_verdict_cache(confirm=True)` on arxiv-research-mcp, then build a fresh digest so all papers are re-judged against my updated `rubric_focus`.

**Inspect the verdict cache**

> Read the `verdict-cache://state` resource from arxiv-research-mcp so I can see which papers have cached verdicts and what scores they got.

**Check which config is active**

> Read the `config://active` resource from arxiv-research-mcp so I can confirm which topic, keywords, and rubric focus the server is currently using.

---

## AI × security

Matches [`examples/config.toml`](config.toml), the shipped default.

**Weekly watch**

> Show me the top 5 new LLM-jailbreak or agentic-attack papers from arXiv's cs.CR category in the last week. Use `research_judge_rubric` and only surface ones that clear both thresholds.

**Focused hunt**

> Call `fetch_candidate_papers(window_days=14, keywords=["indirect prompt injection", "tool poisoning", "agent hijacking"])`. Apply the judge rubric. I'm specifically hunting for new attack classes against tool-using agents.

**Skeptical scan**

> Fetch the top candidates from this week and apply the research judge rubric. Be especially skeptical about anything that claims a "novel framework for LLM safety" without concrete evaluation details or an ablation.

**Threat-model mode**

> Build a digest focused on papers that describe NEW attack classes (not defenses). I care about novelty of the attack surface, not efficacy of mitigations.

---

## Machine learning research

Matches [`examples/topics/machine-learning.toml`](topics/machine-learning.toml).

**Scaling and generalization**

> Use arxiv-research-mcp to fetch cs.LG papers from the last 7 days. Apply the research judge rubric — I'm looking for papers that reveal something surprising about neural network generalization, scaling behavior, or training dynamics.

**Methods over benchmarks**

> Build a weekly digest from cs.LG focused on genuinely novel methods (not benchmark-chasing). Apply the judge rubric and drop anything where the contribution is primarily SOTA on a leaderboard.

**Optimizer watch**

> Fetch cs.LG papers from the last 30 days mentioning optimizers: Adam, AdamW, Muon, Sophia, second-order methods, sign-based updates. Score with the rubric and show me anything with concrete convergence analysis.

**State-space models**

> Hunt for recent work on Mamba, S4, S5, or other state-space model variants. Apply the judge rubric and focus on papers that discuss what SSMs do BETTER than transformers, not just "comparable on benchmark X".

---

## Cryptography

Matches [`examples/topics/cryptography.toml`](topics/cryptography.toml).

**Post-quantum watch**

> Fetch cs.CR papers from the last 30 days with keywords focused on post-quantum: lattice, ring-LWE, module-LWE, Kyber, Dilithium, isogeny, SPHINCS. Apply the judge rubric and surface anything proposing new primitives or efficiency improvements.

**Zero-knowledge deep dive**

> Use arxiv-research-mcp to hunt for new zero-knowledge papers from the last 30 days: SNARKs, STARKs, Plonk-family, folding schemes (Nova, ProtoStar), accumulators, vector commitments. Score with the judge rubric, focusing on practical efficiency.

**MPC and FHE**

> Fetch recent cs.CR papers on multi-party computation and fully homomorphic encryption. Apply the rubric and surface anything with concrete benchmarks against CKKS, BFV, or BGV baselines.

**Cryptanalysis focus**

> Build a digest specifically of cryptanalysis results — papers that BREAK or WEAKEN existing schemes. Apply the judge rubric and drop anything that is just a new construction.

---

## Computational biology

Matches [`examples/topics/quantitative-biology.toml`](topics/quantitative-biology.toml).

**Protein structure and design**

> Fetch q-bio.BM + cs.LG cross-listed papers from the last 14 days. Apply the research judge rubric — I care about novel ML architectures for protein structure prediction and applications to de novo design.

**Drug discovery**

> Use arxiv-research-mcp to survey the last week's papers on molecular generation, drug-target affinity prediction, and equivariant graph neural networks. Filter aggressively for experimental validation mentioned in the abstract.

**Single-cell and genomics**

> Fetch recent papers on single-cell RNA-seq representation learning, batch correction, and trajectory inference. Apply the judge rubric and surface papers that show biological insight, not just ML benchmarks.

**Geometric deep learning**

> Build a digest of SE(3)-equivariant architectures applied to molecules or proteins. Score each paper on whether the equivariance actually pays off vs. a non-equivariant baseline.

---

## Distributed systems

Matches [`examples/topics/distributed-systems.toml`](topics/distributed-systems.toml).

**Consensus and fault tolerance**

> Fetch cs.DC papers from the last 30 days with keywords related to consensus protocols, Byzantine fault tolerance, and leader-based replication. Apply the judge rubric — I want papers with reproducible benchmarks, not pure theory.

**Storage and tail latency**

> Build a digest of cs.PF + cs.OS papers focused on tail latency and storage performance. Use the judge rubric and surface only papers that show real-world measurements (not simulations).

**Database internals**

> Fetch cs.DB papers on transaction processing, new index structures, or HTAP architectures. Apply the rubric. I care about real implementations measured against Postgres, RocksDB, or a production system.

**Kernel and runtime innovations**

> Hunt for cs.OS papers on eBPF, io_uring, unikernels, or new scheduler designs. Apply the judge rubric and filter for papers with concrete benchmarks on commodity hardware.

---

## Debugging and tuning

Patterns for operating the server itself.

**Check what's been surfaced**

> Show me the full cursor state from arxiv-research-mcp so I can see which arXiv IDs have been marked as surfaced.

**Unmark specific papers**

> Call `unmark_papers(arxiv_ids=["2604.12345", "2604.67890"])` on arxiv-research-mcp so those two are re-evaluated next run with my updated rubric focus.

**Dry-run without marking**

> Fetch candidates for the last 7 days with `dedup=False`. Apply the judge rubric and show me what WOULD be surfaced, but DO NOT call `mark_papers_surfaced` — I'm evaluating rubric tuning, not producing a real digest.

**Wider keyword set for one run**

> Call `fetch_candidate_papers(window_days=14, keywords=["agentic", "tool use", "autonomous agent", "multi-agent", "ai agent"])` — overriding my config's keyword set for this one call — then apply the judge rubric.

**Aggressive filtering**

> Build this week's digest, but raise the thresholds: only surface papers where relevance AND quality are both ≥ 9, capped at 3 papers. I only want the very top signal this week.

**See how many verdicts are cached**

> Call `get_cached_verdicts(limit=100)` on arxiv-research-mcp so I can see which papers already have cached scores and whether the rubric hash matches my current config.

**Wipe the cache after rubric changes**

> I just changed my `rubric_focus` in config. Call `clear_verdict_cache(confirm=True)` so all papers get re-scored against the new focus on the next digest run.

**Debugging a bad surface**

> The last digest surfaced a paper I thought was low quality. Fetch the paper's record from the cursor, then re-render the `research_judge_rubric` prompt with just that paper's abstract so I can see what the rubric would tell the LLM to look for. Help me decide what to tighten in `rubric_focus`.
